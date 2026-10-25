"""A/B graphs for the Reel seam (2026-10-25, Omer's LANTERNS MIGHT #4).

B: reel_ab_B.json — extend on the sample-exact source (char1_exact_5s.mov) with Cue's new
   window-context sentence; sampler as in the run-3 graphs (euler/simple 6, DMAD, no shift).
C: reel_ab_C.json — same, with Omer's Director sampler: res_multistep / simple / 25 steps,
   MiniMaxH3SigmaShift 11/4, no turbo LoRA.
A is the existing run-3 clip char1-extend-22-1 (old source, old prompt, run-3 sampler).
"""
import json
import os

import make_reel

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = "promptdeck_tests/sources/char1_exact_5s.mov"


def variant(name, director_sampler):
    make_reel.SOURCE = SOURCE
    g = make_reel.reel("extend")
    g["19"]["inputs"]["filename_prefix"] = f"promptdeck_tests/reel_ab_{name}"
    g["9"]["inputs"]["session"] = f"reel_ab_{name}"
    if director_sampler:
        # model -> SigmaShift(11, 4) -> scheduler + guider; no LoRA; 25 steps res_multistep
        g["60"] = {"class_type": "MiniMaxH3SigmaShift", "_meta": {"title": "Sigma Shift"},
                   "inputs": {"model": ["1", 0], "shift_video": 11.0, "shift_audio": 4.0}}
        g["11"]["inputs"].update(model=["60", 0], steps=25, scheduler="simple")
        g["13"]["inputs"]["model"] = ["60", 0]
        g["10"]["inputs"]["sampler_name"] = "res_multistep"
        del g["5"]  # Turbo LoRA
    return g


if __name__ == "__main__":
    for name, director in (("B", False), ("C", True)):
        path = os.path.join(HERE, f"reel_ab_{name}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(variant(name, director), f, indent=2)
        print("wrote", path)
