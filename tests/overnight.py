#!/usr/bin/env python3
"""Overnight test driver for PromptDeck's Cue and Reel nodes (docs/WINDOWS_RUNBOOK.md §2-5).

GATE: do not queue anything on the Windows box until Omer says LANTERNS MIGHT.
The script does not enforce the gate; `--dry-run` is the first command to run, it queues nothing.

    python tests/overnight.py --dry-run --all          # validate every batch against /object_info
    python tests/overnight.py cue-format               # run one batch, then free VRAM and judge it
    python tests/overnight.py cue-format --all         # run that batch and every one after it
    python tests/overnight.py reel-anchors --judge-only
    python tests/overnight.py --selftest               # offline unit checks, no servers needed

Stdlib only. One batch file = one workflow (API format) + a matrix of values substituted into it.
Outputs land in <out>/<batch>/<item>/ with run.json, judge.json, frames/; per batch results.csv,
index.html and log.txt. Items whose run.json says status ok are skipped on re-runs.
"""
import argparse
import base64
import csv
import hashlib
import html
import itertools
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BATCH_DIR = REPO / "tests" / "batches"
BATCH_ORDER = ["cue-format", "reel-anchors", "cue-video", "reel-refine", "cue-image"]
CLIENT_ID = "promptdeck-overnight"
SCORE_KEYS = ["intent_match", "prompt_match", "identity_match", "seam", "artifacts"]
PRIMARY_BY_KIND = {"video": "intent_match", "image": "intent_match", "prompt": "prompt_match"}
VIDEO_EXT = {".mp4", ".webm", ".mov", ".mkv", ".gif"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp"}
NUMBERING_RE = re.compile(r"^\s*\d{1,4}(?:\s*[.):]|\s+-)\s*")  # same as prompt_deck.py
VAR_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_.]*)\}")

_LOG_FILE = None


def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    if _LOG_FILE:
        try:
            _LOG_FILE.write(line + "\n")
            _LOG_FILE.flush()
        except OSError:
            pass


def read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def http(url, data=None, timeout=30, raw=False):
    """GET (data None) or POST JSON. Returns parsed JSON (or bytes when raw). Raises urllib errors."""
    body = None if data is None else json.dumps(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = resp.read()
    return payload if raw else json.loads(payload.decode("utf-8") or "null")


def http_error_text(err):
    if isinstance(err, urllib.error.HTTPError):
        return f"HTTP {err.code}: {err.read().decode('utf-8', 'replace')[:800]}"
    return f"{type(err).__name__}: {err}"


def resolve(path, base=REPO):
    p = Path(str(path).replace("\\", "/")) if path else None
    return p if p is None or p.is_absolute() else (base / p)


# ---------------------------------------------------------------- intents + matrix

def parse_intents(text):
    """One intent per line; blank lines and # comments skipped; leading `12.` numbering stripped."""
    out = []
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        out.append(NUMBERING_RE.sub("", s).strip())
    return out


def expand_matrix(matrix, base=REPO):
    """Cartesian product of the list-valued keys. Scalars pass through. Returns [(id, values)]."""
    axes, const = [], {}
    for key, val in matrix.items():
        if key.startswith("_") or key == "comment":
            continue
        if key == "intent_file":
            path = resolve(val, base)
            if not path.is_file():
                raise FileNotFoundError(f"intent_file not found: {path}")
            intents = parse_intents(path.read_text(encoding="utf-8", errors="replace"))
            axes.append(("intent", [(i + 1, t) for i, t in enumerate(intents)]))
        elif key == "intents":
            axes.append(("intent", [(i + 1, t) for i, t in enumerate(val)]))
        elif isinstance(val, list):
            axes.append((key, val))
        else:
            const[key] = val
    items = []
    for combo in itertools.product(*[vals for _, vals in axes]) if axes else [()]:
        values = dict(const)
        for (key, _), v in zip(axes, combo):
            if key == "intent":
                values["intent_index"], values["intent"] = v
            else:
                values[key] = v
        items.append((item_id(values, [k for k, _ in axes]), values))
    return items


def item_id(values, axis_keys):
    """Stable id: a slug of the varying values (matrix order) plus a hash of every value."""
    digest = hashlib.sha1(json.dumps(values, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:8]
    bits = []
    for key in axis_keys:
        v = values[key]
        if key == "intent":
            bits.append(f"i{values['intent_index']:02d}")
            continue
        v = v.get("name", "") if isinstance(v, dict) else v
        bits.append(re.sub(r"[^0-9A-Za-z]+", "", str(v).split("/")[-1].split(":")[-1])[:12])
    slug = "-".join(b for b in bits if b)[:48]
    return f"{slug}-{digest}" if slug else digest


# ---------------------------------------------------------------- workflow substitution

def lookup(values, name):
    """`{seed}` or `{seeds}` for the matrix key `seeds`; dotted `{source.file}` when an entry is a dict."""
    cur = values
    for part in name.split("."):
        if isinstance(cur, dict) and part not in cur and part + "s" in cur:
            part += "s"
        if not isinstance(cur, dict) or part not in cur:
            raise KeyError(f"unknown matrix variable {{{name}}}")
        cur = cur[part]
    return cur


def fill_value(value, values):
    """`{var}` alone keeps the variable's type; inside a longer string it is formatted in."""
    if not isinstance(value, str):
        return value
    m = VAR_RE.fullmatch(value)
    return lookup(values, m.group(1)) if m else VAR_RE.sub(lambda mm: str(lookup(values, mm.group(1))), value)


def find_node(graph, ref):
    """Node id by `_meta.title` first (exact, then case-insensitive), then by id."""
    ref = str(ref)
    for nid, node in graph.items():
        if node.get("_meta", {}).get("title") == ref:
            return nid
    for nid, node in graph.items():
        if str(node.get("_meta", {}).get("title", "")).lower() == ref.lower():
            return nid
    if ref in graph:
        return ref
    titles = sorted(str(n.get("_meta", {}).get("title", "")) for n in graph.values())
    raise KeyError(f"node '{ref}' not found; titles: {titles}, ids: {sorted(graph)}")


def substitute(graph, sets, values):
    """Deep-copies the API graph and applies every `set` entry. Missing node or input fails loudly."""
    graph = json.loads(json.dumps(graph))
    for entry in sets:
        if any(lookup(values, k) != v for k, v in (entry.get("when") or {}).items()):
            continue  # entry applies only to items matching its "when" values
        nid = find_node(graph, entry["node"])
        inputs = graph[nid].setdefault("inputs", {})
        name = entry["input"]
        if name not in inputs and not entry.get("create", False):
            raise KeyError(f"node '{entry['node']}' ({graph[nid].get('class_type')}) has no input "
                           f"'{name}'; inputs: {sorted(inputs)}  (add \"create\": true to force)")
        inputs[name] = fill_value(entry["value"], values)
    return graph


def prune_to(graph, keep_refs):
    """Keep only the named output nodes and everything upstream of them (prompt-only runs)."""
    keep, stack = set(), [find_node(graph, r) for r in keep_refs]
    while stack:
        nid = stack.pop()
        if nid in keep:
            continue
        keep.add(nid)
        for v in graph[nid].get("inputs", {}).values():
            if isinstance(v, list) and len(v) == 2 and str(v[0]) in graph:
                stack.append(str(v[0]))
    return {nid: node for nid, node in graph.items() if nid in keep}


def load_batch(name_or_path):
    """A batch name in tests/batches/, or a path (relative to the working directory or the repo)."""
    cands = [Path(name_or_path), REPO / name_or_path, BATCH_DIR / f"{name_or_path}.json"]
    path = next((c for c in cands if c.is_file()), None)
    if path is None:
        raise FileNotFoundError(f"batch file not found: {name_or_path} (tried {[str(c) for c in cands]})")
    batch = read_json(path)
    batch["_name"], batch["_path"] = path.stem, path.resolve()
    return batch


def workflow_path(batch, values):
    wf = batch["workflow"]
    if isinstance(wf, dict):
        key = lookup(values, wf.get("by", "mode"))
        if str(key) not in wf.get("map", {}):
            raise KeyError(f"workflow map has no entry for {wf.get('by', 'mode')}={key!r}")
        wf = wf["map"][str(key)]
    return resolve(wf)


def build_graph(batch, values, cache=None):
    path = workflow_path(batch, values)
    cache = cache if cache is not None else {}
    if path not in cache:
        cache[path] = read_json(path)
    graph = substitute(cache[path], batch.get("set", []), values)
    if batch.get("prune_to"):
        graph = prune_to(graph, batch["prune_to"])
    return graph


# ---------------------------------------------------------------- ComfyUI client

def queue_prompt(comfy, graph):
    return http(f"{comfy}/prompt", {"prompt": graph, "client_id": CLIENT_ID}, timeout=60)


def wait_history(comfy, prompt_id, timeout, poll=2.0):
    """Polls /history/<id> until outputs or an error show up. Returns (status, history_entry)."""
    t0, misses = time.time(), 0
    while time.time() - t0 < timeout:
        time.sleep(poll)
        try:
            hist = http(f"{comfy}/history/{prompt_id}", timeout=30)
            misses = 0
        except (urllib.error.URLError, OSError, ValueError) as err:
            misses += 1
            if misses >= 15:
                return "error", {"error": f"ComfyUI unreachable while polling: {http_error_text(err)}"}
            continue
        entry = (hist or {}).get(prompt_id)
        if not entry:
            continue
        status = entry.get("status", {}) or {}
        if status.get("status_str") == "error":
            msgs = json.dumps(status.get("messages", []))[:2000]
            return ("interrupted" if "interrupted" in msgs else "error"), {"error": msgs, "history": entry}
        if status.get("completed") or entry.get("outputs"):
            return "ok", {"history": entry}
    return "timeout", {"error": f"no result after {timeout}s"}


def walk_outputs(outputs):
    """Yields (node_id, file_dict) for every {filename, subfolder, type} in the history outputs."""
    for nid, ui in (outputs or {}).items():
        for val in (ui or {}).values():
            if isinstance(val, list):
                for f in val:
                    if isinstance(f, dict) and "filename" in f:
                        yield nid, f


def extract_text(outputs, prefer_node=None):
    """Best-effort prompt text from the ui outputs: a list of strings under text-ish keys."""
    def strings(ui):
        for key in ("prompt", "text", "resolved", "string") + tuple(ui):
            val = ui.get(key)
            if isinstance(val, list) and val and all(isinstance(s, str) for s in val):
                return "\n".join(val)
            if isinstance(val, list) and val and isinstance(val[0], dict) and isinstance(val[0].get("text"), str):
                return val[0]["text"]
        return None
    order = ([prefer_node] if prefer_node in (outputs or {}) else []) + list(outputs or {})
    for nid in order:
        text = strings(outputs.get(nid) or {})
        if text:
            return nid, text
    return None, None


def extract_validator(outputs, prefer_node=None):
    """A dict with validator-ish keys (valid / errors / retries / writer / seconds), if the node emits one."""
    order = ([prefer_node] if prefer_node in (outputs or {}) else []) + list(outputs or {})
    for nid in order:
        for val in (outputs.get(nid) or {}).values():
            for d in val if isinstance(val, list) else [val]:
                if isinstance(d, dict) and {"valid", "errors", "retries", "retry_count"} & set(d):
                    return {k: (" | ".join(map(str, v)) if isinstance(v, list) else v) for k, v in d.items()
                            if isinstance(v, (str, int, float, bool, list)) or v is None}
    return None


def download_outputs(comfy, outputs, dest):
    files, seen = [], set()
    for nid, f in walk_outputs(outputs):
        q = urllib.parse.urlencode({"filename": f["filename"], "subfolder": f.get("subfolder", ""),
                                    "type": f.get("type", "output")})
        name = Path(f["filename"]).name
        if name in seen:
            name = f"{nid}_{name}"
        seen.add(name)
        try:
            data = http(f"{comfy}/view?{q}", timeout=600, raw=True)
            (dest / name).write_bytes(data)
            files.append(name)
        except (urllib.error.URLError, OSError) as err:
            log(f"    download failed {f['filename']}: {http_error_text(err)}")
    return files


def free_comfy(comfy):
    try:
        http(f"{comfy}/free", {"unload_models": True, "free_memory": True}, timeout=120)
        log("POST /free ok (models unloaded)")
    except (urllib.error.URLError, OSError, ValueError) as err:
        log(f"POST /free failed (ignored): {http_error_text(err)}")


def item_seams(batch, values):
    spec = (batch.get("judge") or {}).get("seams")
    if isinstance(spec, dict):
        spec = spec.get(str(lookup(values, spec.get("by", "op"))), [])
    return [float(fill_value(s, values)) for s in (spec or [])]


# ---------------------------------------------------------------- batch run

def run_batch(batch, args):
    name, out_dir = batch["_name"], Path(args.out) / batch["_name"]
    out_dir.mkdir(parents=True, exist_ok=True)
    items = expand_matrix(batch.get("matrix", {}))
    prefer = batch.get("outputs", {}).get("node")
    timeout = float(batch.get("timeout_seconds", args.timeout))
    log(f"== batch {name}: {len(items)} items -> {out_dir}")
    cache, done, failures, offline = {}, 0, 0, 0
    for n, (iid, values) in enumerate(items, 1):
        item_dir = out_dir / iid
        run_path = item_dir / "run.json"
        if run_path.is_file() and read_json(run_path).get("status") == "ok":
            done += 1
            continue
        item_dir.mkdir(parents=True, exist_ok=True)
        run = {"id": iid, "batch": name, "values": values, "status": "queued", "seams": item_seams(batch, values),
               "queued_at": time.strftime("%Y-%m-%d %H:%M:%S"), "prompt_text": None, "validator": None,
               "files": [], "errors": [], "seconds": None}
        log(f"[{n}/{len(items)}] {iid}  {json.dumps({k: v for k, v in values.items() if k != 'intent'})}")
        t0 = time.time()
        try:
            graph = build_graph(batch, values, cache)
            pnode = find_node(graph, prefer) if prefer else None
            resp = queue_prompt(args.comfy, graph)
            if resp.get("node_errors"):
                raise RuntimeError(f"node_errors: {json.dumps(resp['node_errors'])[:1500]}")
            prompt_id = resp["prompt_id"]
            run["prompt_id"], offline = prompt_id, 0
            status, info = wait_history(args.comfy, prompt_id, timeout)
            run["status"] = status
            hist = info.get("history") or {}
            if info.get("error"):
                run["errors"].append(info["error"])
            if hist:
                write_json(item_dir / "history.json", hist)
                outputs = hist.get("outputs") or {}
                run["prompt_node"], run["prompt_text"] = extract_text(outputs, pnode)
                run["validator"] = extract_validator(outputs, pnode)
                if not run["prompt_text"] and isinstance(run["validator"], dict) and run["validator"].get("prompt"):
                    run["prompt_text"] = run["validator"]["prompt"]  # Cue's ui carries the prompt in its dict
                run["files"] = download_outputs(args.comfy, outputs, item_dir)
                if run["prompt_text"]:
                    (item_dir / "prompt.txt").write_text(run["prompt_text"], encoding="utf-8")
        except (urllib.error.URLError, OSError) as err:
            run["status"] = "error"
            run["errors"].append(f"queue failed: {http_error_text(err)}")
            offline += 1
        except (KeyError, RuntimeError, ValueError) as err:
            run["status"] = "error"
            run["errors"].append(str(err))
        run["seconds"] = round(time.time() - t0, 1)
        write_json(run_path, run)
        if run["status"] == "ok":
            done += 1
            log(f"    ok in {run['seconds']}s, files={run['files']}, prompt={len(run['prompt_text'] or '')} chars")
        else:
            failures += 1
            log(f"    {run['status']}: {' | '.join(run['errors'])[:300]}")
        if offline >= 3:
            log("ComfyUI unreachable three times in a row; stopping this batch (re-run to resume).")
            break
    log(f"== batch {name} done: {done} ok, {failures} failed this run")
    return done, failures


# ---------------------------------------------------------------- frames + judge

def video_duration(ffmpeg, video):
    proc = subprocess.run([ffmpeg, "-i", str(video)], capture_output=True, text=True, errors="replace")
    m = re.search(r"Duration: (\d+):(\d+):(\d+\.?\d*)", proc.stderr)
    return float(m.group(1)) * 3600 + float(m.group(2)) * 60 + float(m.group(3)) if m else None


def gather_frames(item_dir, seams, n_even=8, n_seam=4, exclude=()):
    """Returns (frames_source, [(label, path)]). ffmpeg extraction when possible, saved stills otherwise.
    `exclude`: file names that are inputs copied alongside the outputs (Reel source clips) — run 1
    judged the source clip because it sorted first."""
    videos = sorted(p for p in item_dir.iterdir() if p.suffix.lower() in VIDEO_EXT and p.name not in set(exclude))
    n_even = max(0, n_even)
    ffmpeg = shutil.which("ffmpeg")
    if videos and ffmpeg:
        video, fdir = videos[0], item_dir / "frames"
        fdir.mkdir(exist_ok=True)
        dur = video_duration(ffmpeg, video)
        if dur:
            times = [(f"t{i + 1:02d}", (i + 0.5) * dur / n_even) for i in range(n_even)]
            for s_i, s in enumerate(seams or []):
                offsets = [(k - (n_seam - 1) / 2) * 0.25 for k in range(n_seam)]
                times += [(f"seam{s_i + 1}_{k + 1}", min(max(s + o, 0.0), dur - 0.05)) for k, o in enumerate(offsets)]
            frames = []
            for label, t in times:
                dst = fdir / f"{label}.jpg"
                if not dst.is_file():
                    subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-ss", f"{t:.3f}", "-i", str(video),
                                    "-frames:v", "1", "-q:v", "3", str(dst)], capture_output=True)
                if dst.is_file():
                    frames.append((label, dst))
            if frames:
                return f"ffmpeg:{video.name}", frames
    stills = sorted(p for p in item_dir.iterdir() if p.suffix.lower() in IMAGE_EXT)
    if stills and n_even:
        step = max(1, len(stills) / n_even)
        picked = [stills[int(i * step)] for i in range(min(n_even, len(stills)))]
        return "saved_frames", [(f"t{i + 1:02d}", p) for i, p in enumerate(picked)]
    return "none", []


def ref_pictures(batch, values, limit=3):
    spec = (batch.get("judge") or {}).get("refs")
    if not spec:
        return []
    folder = resolve(fill_value(spec, values))
    if folder.is_file():
        return [folder]
    return sorted(p for p in folder.glob("*") if p.suffix.lower() in IMAGE_EXT)[:limit] if folder.is_dir() else []


def parse_scores(raw):
    """JSON (possibly wrapped in prose) -> (scores dict, scored bool). Ints clamped to 1-5, else None."""
    data = None
    for cand in (raw, raw[raw.find("{"):raw.rfind("}") + 1] if "{" in raw else ""):
        try:
            data = json.loads(cand)
            break
        except (ValueError, TypeError):
            continue
    if not isinstance(data, dict):
        return {k: None for k in SCORE_KEYS} | {"note": None}, False
    scores = {}
    for k in SCORE_KEYS:
        v = data.get(k)
        try:
            scores[k] = min(5, max(1, int(round(float(v))))) if v is not None else None
        except (TypeError, ValueError):
            scores[k] = None
    scores["note"] = str(data.get("note", ""))[:500] or None
    return scores, any(scores[k] is not None for k in SCORE_KEYS)


def judge_item(batch, run, item_dir, args, system_prompt):
    kind = (batch.get("judge") or {}).get("kind", "video")
    seams = run.get("seams") or []
    # input clips the driver copied next to the outputs (Reel sources) must not be judged
    exclude = {Path(str(v)).name for v in walk_values(run["values"]) if str(v).lower().endswith(tuple(VIDEO_EXT))}
    if kind == "prompt":
        source, frames = "none", []
    elif kind == "seam":  # only the frames around each cut; no intent, no prompt
        source, frames = gather_frames(item_dir, seams, n_even=0, exclude=exclude)
    else:
        source, frames = gather_frames(item_dir, seams, exclude=exclude)
    refs = ref_pictures(batch, run["values"])
    images, legend = [], []
    for label, p in frames:
        images.append(base64.b64encode(p.read_bytes()).decode("ascii"))
        legend.append(f"image {len(images)}: {'seam frame ' + label if label.startswith('seam') else 'clip frame ' + label}")
    for p in refs:
        images.append(base64.b64encode(p.read_bytes()).decode("ascii"))
        legend.append(f"image {len(images)}: reference picture of the person ({p.name})")
    if kind == "seam":
        user = [f"KIND: {kind}", f"SEAMS (seconds): {seams or 'none'}",
                f"REFERENCES: {len(refs)} picture(s) attached" if refs else "REFERENCES: none, score identity_match null",
                "IMAGES: " + ("; ".join(legend) if legend else "none, no seam frames could be extracted")]
    else:
        user = [f"KIND: {kind}", f"INTENT: {run['values'].get('intent', '(none)')}",
                f"PROMPT:\n{prompt_of(run) or '(no prompt text captured)'}",
                f"SEAMS (seconds): {seams or 'none'}",
                f"REFERENCES: {len(refs)} picture(s) attached" if refs else "REFERENCES: none, score identity_match null",
                "IMAGES: " + ("; ".join(legend) if legend else "none, this is a prompt-only item")]
    # num_ctx: without it Ollama allocates the model's full 256k context (64 GB of KV for the
    # 32B) and offloads most layers to CPU — 120 s per item. 32k covers ~19 images + prompt.
    # keep_alive: 0 evicted the 20 GB model after every answer; keep it warm across the batch.
    payload = {"model": args.judge_model or batch.get("judge_model"), "stream": False, "format": "json",
               "keep_alive": "30m", "options": {"temperature": 0, "num_ctx": 32768},
               "messages": [{"role": "system", "content": system_prompt},
                            {"role": "user", "content": "\n".join(user), "images": images}]}
    result = {"id": run["id"], "model": payload["model"], "kind": kind, "frames_source": source,
              "frames": [p.relative_to(item_dir).as_posix() for _, p in frames],
              "refs": [str(p) for p in refs], "scored": False, "raw": None, "error": None}
    try:
        t0 = time.time()
        resp = http(f"{args.ollama}/api/chat", payload, timeout=args.judge_timeout)
        result["raw"] = (resp.get("message") or {}).get("content", "")
        result["seconds"] = round(time.time() - t0, 1)
        scores, scored = parse_scores(result["raw"] or "")
        result.update(scores)
        result["scored"] = scored
    except (urllib.error.URLError, OSError, ValueError) as err:
        result["error"] = http_error_text(err)
        result.update({k: None for k in SCORE_KEYS})
    write_json(item_dir / "judge.json", result)
    return result


def walk_values(obj):
    """Every scalar inside a matrix values dict (dict entries such as source.file included)."""
    if isinstance(obj, dict):
        for v in obj.values():
            yield from walk_values(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from walk_values(v)
    else:
        yield obj


def prompt_of(run):
    """Prompt text of a run.json; items written before the validator fallback keep it in validator.prompt."""
    return run.get("prompt_text") or (run.get("validator") or {}).get("prompt") or ""


def judge_batch(batch, args):
    out_dir = Path(args.out) / batch["_name"]
    system_prompt = (REPO / "tests" / "judge_prompt.md").read_text(encoding="utf-8")
    model, runs = args.judge_model or batch.get("judge_model"), []
    if not model:
        log("no judge model (batch judge_model or --judge-model): reports from run.json only")
    for run_path in sorted(out_dir.glob("*/run.json")):
        run, item_dir = read_json(run_path), run_path.parent
        judge_path = item_dir / "judge.json"
        judged = read_json(judge_path) if judge_path.is_file() else None
        if model and run.get("status") == "ok" and (args.rejudge or not judged or not judged.get("scored")):
            log(f"judge {run['id']}")
            judged = judge_item(batch, run, item_dir, args, system_prompt)
            log(f"    {'scored ' + json.dumps({k: judged.get(k) for k in SCORE_KEYS}) if judged['scored'] else 'UNSCORED ' + str(judged.get('error') or (judged.get('raw') or '')[:120])}")
        runs.append((run, judged, item_dir))
    write_report(batch, runs, out_dir)
    return runs


# ---------------------------------------------------------------- report: results.csv + index.html

def primary_key(batch):
    j = batch.get("judge") or {}
    return j.get("primary") or PRIMARY_BY_KIND.get(j.get("kind", "video"), "intent_match")


def write_report(batch, runs, out_dir):
    primary = primary_key(batch)
    runs = sorted(runs, key=lambda r: (-(((r[1] or {}).get(primary)) or 0), r[0]["id"]))
    var_keys = sorted({k for run, _, _ in runs for k in run["values"]} - {"intent"})
    val_keys = sorted({k for run, _, _ in runs for k in (run.get("validator") or {})})
    header = (["id"] + var_keys + ["intent", "status", "seconds", "prompt_chars"] + [f"v_{k}" for k in val_keys]
              + SCORE_KEYS + ["note", "scored", "frames_source", "path", "video"])
    rows = []
    for run, judged, item_dir in runs:
        judged = judged or {}
        video = next((f for f in run.get("files", []) if Path(f).suffix.lower() in VIDEO_EXT), "")
        row = {"id": run["id"], "intent": run["values"].get("intent", ""), "status": run["status"],
               "seconds": run.get("seconds"), "prompt_chars": len(prompt_of(run)),
               "note": judged.get("note"), "scored": judged.get("scored"), "frames_source": judged.get("frames_source"),
               "path": item_dir.as_posix(), "video": (item_dir / video).as_posix() if video else ""}
        row.update({k: run["values"].get(k) for k in var_keys})
        row.update({f"v_{k}": (run.get("validator") or {}).get(k) for k in val_keys})
        row.update({k: judged.get(k) for k in SCORE_KEYS})
        rows.append(row)
    with open(out_dir / "results.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        w.writerows(rows)
    write_html(batch, runs, rows, out_dir, primary)


def write_html(batch, runs, rows, out_dir, primary):
    e = html.escape
    n_ok = sum(1 for r in rows if r["status"] == "ok")
    means = []
    for k in SCORE_KEYS:
        vals = [r[k] for r in rows if isinstance(r.get(k), int)]
        means.append(f"{k} {sum(vals) / len(vals):.2f} (n={len(vals)})" if vals else f"{k} -")
    parts = [f"<!doctype html><meta charset='utf-8'><title>{e(batch['_name'])}</title><style>"
             "body{font:14px system-ui;margin:16px;background:#fafafa}.i{display:flex;gap:12px;padding:10px;"
             "margin:8px 0;background:#fff;border:1px solid #ddd}.i img{height:110px}.i video{height:220px}"
             ".s b{margin-right:10px}.bad{background:#fee}.v{color:#666;font-size:12px}pre{white-space:pre-wrap;"
             "font-size:12px;max-width:900px}</style>"
             f"<h1>{e(batch['_name'])}</h1><p>{len(rows)} items, {n_ok} ok, sorted by <b>{e(primary)}</b>. "
             f"means: {e(', '.join(means))}. judge: {e(str(batch.get('judge_model')))}</p>"]
    for (run, judged, item_dir), row in zip(runs, rows):
        judged, rel = judged or {}, item_dir.name
        thumbs = "".join(f"<img src='{e(rel)}/{e(fr)}' title='{e(fr)}'>" for fr in (judged.get("frames") or [])[:4])
        if not thumbs:
            thumbs = "".join(f"<img src='{e(rel)}/{e(f)}'>" for f in run.get("files", [])
                             if Path(f).suffix.lower() in IMAGE_EXT)[:4]
        video = next((f for f in run.get("files", []) if Path(f).suffix.lower() == ".mp4"), None)
        media = f"<video controls preload='none' src='{e(rel)}/{e(video)}'></video>" if video else ""
        scores = "".join(f"<b>{e(k)} {judged.get(k) if judged.get(k) is not None else '-'}</b>" for k in SCORE_KEYS)
        vals = ", ".join(f"{e(k)}={e(str(v))}" for k, v in run["values"].items() if k != "intent")
        parts.append(
            f"<div class='i{'' if row['status'] == 'ok' else ' bad'}'>{media}<div>{thumbs}<div class='s'>{scores}"
            f"</div><div><b>{e(run['id'])}</b> <span class='v'>{row['status']} {row['seconds']}s | {vals}</span></div>"
            f"<div>{e(run['values'].get('intent', ''))}</div><div class='v'>{e(judged.get('note') or '')}"
            f" {'' if judged.get('scored') or not judged else '(unscored)'} {e(' | '.join(run.get('errors') or []))[:300]}</div>"
            f"<details><summary>prompt ({len(prompt_of(run))} chars)</summary>"
            f"<pre>{e(prompt_of(run))}</pre></details><div class='v'>{e(rel)}</div></div></div>")
    (out_dir / "index.html").write_text("\n".join(parts), encoding="utf-8")


# ---------------------------------------------------------------- dry run

def dry_run(batches, args):
    problems = 0
    try:
        info = http(f"{args.comfy}/object_info", timeout=60)
        log(f"GET /object_info: {len(info)} node classes")
    except (urllib.error.URLError, OSError, ValueError) as err:
        info = None
        log(f"WARNING /object_info unreachable ({http_error_text(err)}): checking files and substitution only")
    for batch in batches:
        try:
            items = expand_matrix(batch.get("matrix", {}))
        except (FileNotFoundError, KeyError) as err:
            log(f"{batch['_name']}: FAIL {err}")
            problems += 1
            continue
        seen_wf, cache = set(), {}
        for iid, values in items:
            try:
                wf = workflow_path(batch, values)
                graph = build_graph(batch, values, cache)
                if batch.get("outputs", {}).get("node"):
                    find_node(graph, batch["outputs"]["node"])
                if (batch.get("judge") or {}).get("refs") and not ref_pictures(batch, values):
                    log(f"{batch['_name']}: WARN no reference pictures for {iid}")
            except (FileNotFoundError, KeyError, ValueError) as err:
                log(f"{batch['_name']}: FAIL item {iid}: {err}")
                problems += 1
                continue
            if wf in seen_wf or not info:
                continue
            seen_wf.add(wf)
            for nid, node in graph.items():
                spec = info.get(node.get("class_type"))
                if not spec:
                    log(f"{batch['_name']}: FAIL {wf.name} node {nid}: unknown class {node.get('class_type')}")
                    problems += 1
                    continue
                allowed = {}
                for group in ("required", "optional"):
                    allowed.update(spec.get("input", {}).get(group, {}))
                # V3 autogrow inputs (e.g. MiniMaxH3ReferenceToVideo.ref_images) are one entry in
                # /object_info but travel as dotted ids in prompt JSON: "<id>.<prefix><i>" or
                # "<id>.<name>" (comfy_api/latest/_io.py finalize_prefix). Admit those too.
                for key, entry in list(allowed.items()):
                    opts = entry[1] if isinstance(entry, list) and len(entry) > 1 and isinstance(entry[1], dict) else {}
                    tpl = opts.get("template") or {}
                    if entry and entry[0] == "COMFY_AUTOGROW_V3" and tpl:
                        names = tpl.get("names") or [f"{tpl.get('prefix', '')}{i}" for i in range(int(tpl.get("max", 0)))]
                        for sub in names:
                            allowed.setdefault(f"{key}.{sub}", ["*"])
                for name, val in node.get("inputs", {}).items():
                    if name not in allowed and name not in spec.get("input", {}).get("hidden", {}):
                        log(f"{batch['_name']}: FAIL {wf.name} node {nid} ({node.get('class_type')}): no input '{name}'")
                        problems += 1
                    elif name in allowed and isinstance(allowed[name][0], list) and not isinstance(val, list) \
                            and val not in allowed[name][0] \
                            and not (REPO.parent.parent / "input" / str(val)).is_file():
                        # file combos (LoadImage/LoadVideo/LoadLatent) list only input/'s top level;
                        # ComfyUI itself validates those by path on disk (VALIDATE_INPUTS ->
                        # folder_paths.exists_annotated_filepath), so subfolder paths are fine.
                        log(f"{batch['_name']}: FAIL {wf.name} node {nid} input '{name}': {val!r} not in combo "
                            f"{allowed[name][0][:12]}")
                        problems += 1
        log(f"{batch['_name']}: {len(items)} items, workflows {sorted(p.name for p in seen_wf) or '(unverified)'}, "
            f"judge model {batch.get('judge_model')}")
    log(f"dry run: {problems} problem(s), nothing queued")
    return problems


# ---------------------------------------------------------------- selftest

def selftest():
    import tempfile
    intents = parse_intents("# header\n\n1. a man runs\n  2) \"Merhaba\" she says\n\nplain line\n")
    assert intents == ["a man runs", '"Merhaba" she says', "plain line"], intents
    items = expand_matrix({"intents": ["x", "y"], "modes": ["i2v", "ref2v"], "seeds": [1, 2, 3], "detail": "long"})
    assert len(items) == 12 and lookup(items[0][1], "mode") == "i2v" and items[0][1]["detail"] == "long"
    assert items[0][0] == expand_matrix({"intents": ["x", "y"], "modes": ["i2v", "ref2v"], "seeds": [1, 2, 3],
                                         "detail": "long"})[0][0], "ids must be stable"
    assert len({i for i, _ in items}) == 12, "ids must be unique"
    graph = {"3": {"class_type": "PromptDeckCue", "_meta": {"title": "Cue"},
                   "inputs": {"intent": "", "mode": "t2v", "seed": 0, "image": ["4", 0]}},
             "4": {"class_type": "LoadImage", "inputs": {"image": "a.png"}},
             "9": {"class_type": "SaveVideo", "_meta": {"title": "Save"}, "inputs": {"video": ["3", 0]}}}
    sets = [{"node": "Cue", "input": "intent", "value": "{intent}"}, {"node": "cue", "input": "mode", "value": "{mode}"},
            {"node": "3", "input": "seed", "value": "{seed}"}, {"node": "4", "input": "image", "value": "refs/{mode}.png"}]
    g = substitute(graph, sets, items[5][1])
    assert g["3"]["inputs"] == {"intent": "x", "mode": "ref2v", "seed": 3, "image": ["4", 0]}, g["3"]
    assert g["4"]["inputs"]["image"] == "refs/ref2v.png" and graph["3"]["inputs"]["intent"] == ""
    pairs = expand_matrix({"sources": [{"name": "anna", "file": "a.mp4"}], "ops": ["extend", "bridge"]})
    assert pairs[1][0].startswith("anna-bridge-"), pairs  # slug: varying values in matrix order
    g = substitute(graph, [{"node": "Cue", "input": "intent", "value": "{source.file}", "when": {"op": "bridge"}}],
                   pairs[0][1])
    assert g["3"]["inputs"]["intent"] == ("a.mp4" if pairs[0][1]["ops"] == "bridge" else "")
    for bad in ([{"node": "Nope", "input": "intent", "value": 1}], [{"node": "Cue", "input": "nope", "value": 1}],
                [{"node": "Cue", "input": "intent", "value": "{missing}"}]):
        try:
            substitute(graph, bad, items[0][1])
            raise AssertionError(f"should have failed: {bad}")
        except KeyError:
            pass
    assert set(prune_to(graph, ["Cue"])) == {"3", "4"}
    s, ok = parse_scores('Sure: {"intent_match": 4, "prompt_match": "3", "identity_match": 9, "seam": null, '
                         '"artifacts": 0.6, "note": "fine"} done')
    assert ok and (s["intent_match"], s["prompt_match"], s["identity_match"], s["seam"], s["artifacts"]) == (4, 3, 5, None, 1)
    s, ok = parse_scores("not json at all")
    assert not ok and s["intent_match"] is None
    batch = {"_name": "selftest", "judge": {"kind": "video", "primary": "seam"}, "judge_model": "m"}
    with tempfile.TemporaryDirectory() as tmp:
        out, runs = Path(tmp), []
        for n, (iid, values) in enumerate(items[:3]):
            d = out / iid
            d.mkdir()
            run = {"id": iid, "values": values, "status": "ok" if n else "error", "seconds": 3.5, "files": ["a.mp4"],
                   "prompt_text": "<b>&", "validator": {"valid": True, "retries": n}, "errors": []}
            judged = {"scored": True, "seam": 5 - n, "intent_match": 3, "prompt_match": None, "identity_match": 2,
                      "artifacts": 4, "note": "ok", "frames": ["frames/t01.jpg"]} if n < 2 else None
            runs.append((run, judged, d))
        write_report(batch, runs, out)
        rows = list(csv.DictReader(open(out / "results.csv", encoding="utf-8")))
        assert [r["seam"] for r in rows] == ["5", "4", ""] and rows[0]["v_retries"] == "0" and rows[0]["modes"] == "i2v"
        page = (out / "index.html").read_text(encoding="utf-8")
        assert "&lt;b&gt;&amp;" in page and "<video" in page and "sorted by <b>seam</b>" in page
    print("selftest ok: intents, matrix, substitution, prune, scores, csv/html")


# ---------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("batch", nargs="?", help="batch name in tests/batches/ or a path to a batch json")
    ap.add_argument("--all", action="store_true", help="continue with the following batches in runbook order")
    ap.add_argument("--dry-run", action="store_true", help="validate against /object_info, queue nothing")
    ap.add_argument("--judge-only", action="store_true", help="skip queueing; judge existing items and rebuild reports")
    ap.add_argument("--rejudge", action="store_true", help="with --judge-only: re-score items that already have judge.json")
    ap.add_argument("--no-judge", action="store_true", help="queue only; judge everything later with --judge-only --all")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--comfy", default="http://127.0.0.1:8188")
    ap.add_argument("--ollama", default="http://127.0.0.1:11434")
    ap.add_argument("--out", default=str(REPO.parent.parent / "output" / "promptdeck_tests"),
                    help="output root (default ../../output/promptdeck_tests relative to the repo)")
    ap.add_argument("--judge-model", default=None, help="overrides the batch file's judge_model")
    ap.add_argument("--timeout", type=float, default=1800, help="seconds to wait for one item")
    ap.add_argument("--judge-timeout", type=float, default=1800)
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    if not args.batch and not (args.all and args.dry_run):
        ap.error("give a batch name (or --dry-run --all)")
    names = BATCH_ORDER if args.all and not args.batch else [args.batch]
    if args.all and args.batch in BATCH_ORDER:
        names = BATCH_ORDER[BATCH_ORDER.index(args.batch):]
    batches = [load_batch(n) for n in names]
    if args.dry_run:
        return 1 if dry_run(batches, args) else 0
    global _LOG_FILE
    freed = False
    for batch in batches:
        out_dir = Path(args.out) / batch["_name"]
        out_dir.mkdir(parents=True, exist_ok=True)
        _LOG_FILE = open(out_dir / "log.txt", "a", encoding="utf-8")
        try:
            ran = False
            if not args.judge_only:
                run_batch(batch, args)
                ran = True
                if args.no_judge:
                    log("judging deferred (--no-judge); run with --judge-only later")
                    continue
            # The judge needs the card: free Comfy after a run, and once up front in
            # --judge-only mode (Comfy may still hold a model from an earlier session).
            if ran or not freed:
                free_comfy(args.comfy)
                freed = True
            judge_batch(batch, args)
            log(f"report: {out_dir / 'index.html'}")
        finally:
            _LOG_FILE.close()
            _LOG_FILE = None
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    sys.exit(main())
