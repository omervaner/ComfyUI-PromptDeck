"""Grow templates/palette.md with a local writer, so Cue's seeded suggestions stop repeating.

For every "## axis" in the palette, asks an Ollama model for new lines in rounds, each round
steered by a random angle (a region, a trade, a decade, a weather...) so the batches differ,
dedupes against what is already there (case- and punctuation-insensitive), and appends.

    python tools/make_palette.py --writer huihui_ai/qwen3-vl-abliterated:32b-instruct-q4_K_M \
        --per-round 40 --rounds 25            # ~1000 new lines per axis
    python tools/make_palette.py --axes setting,light --rounds 5 --dry-run

Stdlib only. Runs anywhere Ollama runs; an overnight job on the 5090 box is the idea.
"""
import argparse
import json
import os
import random
import re
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PALETTE = os.path.join(os.path.dirname(HERE), "templates", "palette.md")

ANGLES = [
    "a specific country or region", "a trade or profession", "a decade of the last century", "a time of day",
    "a kind of weather", "a public institution", "transport", "a sport or hobby", "a religious or civic ritual",
    "the sea or a river", "a small shop", "an industrial place", "a domestic room", "a festival or market",
    "a place for children", "a place for the old", "a place you wait in", "a place that is closing down",
    "agriculture", "night work", "the very rich", "the very poor", "a border or checkpoint", "a hospital",
    "a school", "a wedding", "a funeral", "heat", "cold", "a mountain", "a desert", "a forest", "a harbour",
    "a motorway", "a village", "a megacity", "an island", "a basement", "a rooftop", "a vehicle interior",
]

AXIS_BRIEF = {
    "setting": "a concrete place as a short noun phrase with one telling detail (e.g. 'a laundromat with one machine running')",
    "time_weather": "a time of day combined with weather or season, concrete",
    "light": "a light source, its direction and quality, concrete",
    "camera": "a camera distance, lens feel and angle in one phrase",
    "finish": "a photographic or print medium and its finish",
    "mood": "an emotional register expressed through something visible, not an adjective alone",
    "subject_person": "a person as a short noun phrase: age range, build, one piece of clothing, one detail; any gender, any origin",
    "subject_other": "a creature, vehicle, machine or plant as a short noun phrase with one detail",
    "object": "one hand-sized object with a detail of wear or use",
    "colour": "a colour pairing or palette as it would appear in a scene",
}


def norm(line):
    return re.sub(r"[^a-z0-9 ]+", "", line.lower()).strip()


def read_palette():
    head, axes, order, current = [], {}, [], None
    with open(PALETTE, "r", encoding="utf-8") as f:
        for line in f.read().splitlines():
            if line.startswith("## "):
                current = line[3:].strip().lower()
                order.append(current)
                axes[current] = []
            elif current is None:
                head.append(line)
            elif line.startswith("- ") and line[2:].strip():
                axes[current].append(line[2:].strip())
    return head, order, axes


def write_palette(head, order, axes):
    tmp = PALETTE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(head).rstrip() + "\n")
        for axis in order:
            f.write(f"\n## {axis}\n")
            for line in axes[axis]:
                f.write(f"- {line}\n")
    os.replace(tmp, PALETTE)


def ask(endpoint, model, axis, brief, angle, n, avoid, seed):
    system = ("You generate short, concrete, varied fragments for an image-prompt palette. Adults only, "
              "uncensored. Answer as JSON: {\"lines\": [\"...\", ...]}. Each line is one fragment of 4 to 12 "
              "words, lower case, no trailing period, no two alike, nothing generic.")
    user = (f"Axis: {axis}. Each line is {brief}. Angle for this batch: {angle}. Give {n} lines. "
            f"Do not reuse any of these: {'; '.join(avoid[-60:])}")
    body = {"model": model, "stream": False, "keep_alive": "10m", "format": {
        "type": "object", "properties": {"lines": {"type": "array", "items": {"type": "string", "minLength": 4}}},
        "required": ["lines"]},
        "options": {"temperature": 1.0, "seed": seed, "num_predict": 60 * n, "repeat_penalty": 1.1},
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    req = urllib.request.Request(endpoint.rstrip("/") + "/api/chat", data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=600) as r:
        data = json.loads(r.read().decode("utf-8"))
    try:
        lines = json.loads(data["message"]["content"]).get("lines", [])
    except (KeyError, ValueError):
        return []
    return [" ".join(str(x).split()).strip().rstrip(".") for x in lines if isinstance(x, str)]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--writer", default="huihui_ai/qwen3-vl-abliterated:32b-instruct-q4_K_M")
    ap.add_argument("--endpoint", default="http://127.0.0.1:11434")
    ap.add_argument("--axes", default="", help="comma list; default all")
    ap.add_argument("--per-round", type=int, default=40)
    ap.add_argument("--rounds", type=int, default=10)
    ap.add_argument("--seed", type=int, default=int(time.time()) % 100000)
    ap.add_argument("--dry-run", action="store_true", help="ask once per axis, print, write nothing")
    a = ap.parse_args(argv)
    head, order, axes = read_palette()
    wanted = [x.strip().lower() for x in a.axes.split(",") if x.strip()] or order
    rng = random.Random(a.seed)
    total_added = 0
    for axis in wanted:
        if axis not in axes:
            print(f"skip unknown axis {axis!r}; have {order}")
            continue
        seen = {norm(x) for x in axes[axis]}
        added = 0
        for rnd in range(1 if a.dry_run else a.rounds):
            angle = rng.choice(ANGLES)
            try:
                lines = ask(a.endpoint, a.writer, axis, AXIS_BRIEF.get(axis, "a short concrete fragment"),
                            angle, a.per_round, axes[axis], a.seed + rnd)
            except Exception as e:
                print(f"{axis} round {rnd + 1}: {e}")
                continue
            fresh = []
            for line in lines:
                key = norm(line)
                if key and key not in seen and 3 <= len(line.split()) <= 14:
                    seen.add(key)
                    fresh.append(line)
            if a.dry_run:
                print(f"--- {axis} ({angle}):\n  " + "\n  ".join(fresh[:12]))
                break
            axes[axis].extend(fresh)
            added += len(fresh)
            print(f"[{time.strftime('%H:%M:%S')}] {axis} round {rnd + 1}/{a.rounds} ({angle}): +{len(fresh)} -> {len(axes[axis])}")
            write_palette(head, order, axes)  # save after every round; safe to interrupt
        total_added += added
    if not a.dry_run:
        print(f"done: +{total_added} lines; " + ", ".join(f"{x}={len(axes[x])}" for x in order))
    return 0


if __name__ == "__main__":
    sys.exit(main())
