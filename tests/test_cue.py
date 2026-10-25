"""Offline tests for Cue: the JSON protocol, the budgets, and a full run with a fake writer.
No Ollama, no ComfyUI, no pictures.  Run:  python3 tests/test_cue.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cue  # noqa: E402

cue._CACHE_PATH = os.path.join(tempfile.mkdtemp(), "cue_cache.json")
cue._send = lambda *a, **k: None
RESULTS = []


def ok(name, cond, note=""):
    RESULTS.append((name, bool(cond)))
    print(("ok   " if cond else "FAIL ") + name + (f"  ({note})" if note and not cond else ""))


def test_parse_json():
    full = json.dumps({"subjects": [{"token": "Woman", "description": "a woman in her forties."}],
                       "beats": [{"start_seconds": 0, "action": "she turns"}, {"start_seconds": 2.5, "action": "she speaks"}],
                       "integrated_multimodal_description": "[Shot 1] {woman} turns.", "overall_soundscape": "rain",
                       "non_diegetic_music": "none"})
    d = cue.parse_json_answer("Sure:\n" + full, cue.H3_FIELDS_BASE)
    ok("json: subjects slugged", d["subjects"] == {"woman": "a woman in her forties"})
    ok("json: beats parsed", [b["start"] for b in d["beats"]] == [0.0, 2.5])
    ok("json: fields", list(d["fields"]) == cue.H3_FIELDS_BASE)
    cut = full[: full.index("overall_soundscape") + 40]
    d = cue.parse_json_answer(cut, cue.H3_FIELDS_BASE)
    ok("json: truncated salvage keeps imd", d and d["fields"].get("integrated_multimodal_description") == "[Shot 1] {woman} turns.")
    ok("json: truncated salvage keeps subjects+beats", d and d["subjects"] and d["beats"] and len(d["beats"]) == 2)
    ok("json: plain text -> None", cue.parse_json_answer("integrated_multimodal_description: x", cue.H3_FIELDS_BASE) is None)
    esc = json.dumps({"prompt": 'She says "go".\nNew line.'})
    ok("json: escapes", cue.parse_json_answer(esc, ["prompt"])["fields"]["prompt"] == 'She says "go".\nNew line.')


def test_preview():
    partial = '{"subjects":[{"token":"man","description":"a man"}],"beats":[{"start_seconds":0,"action":"x"}],"integrated_multimodal_description":"[Shot 1] He lights a cig'
    ok("preview: shows field so far", cue.json_stream_preview(partial) == "integrated_multimodal_description: [Shot 1] He lights a cig")
    ok("preview: beats stage", cue.json_stream_preview('{"subjects":[],"beats":[{"start_seconds":0') == "planning beats…")
    ok("preview: plain text passthrough", cue.json_stream_preview("hello") == "hello")


def test_budgets():
    ok("beat budget 5s=2", cue.beat_budget(5.17) == 2)
    ok("beat budget 10s=4", cue.beat_budget(10.0) == 4)
    ok("shot budget", (cue.shot_budget(5.17), cue.shot_budget(10.0), cue.shot_budget(15.08)) == (1, 2, 3))
    e, w = cue.validate_beats([{"start": 0, "action": "a"}, {"start": 1, "action": "b"}, {"start": 2, "action": "c"}], 5.17, 2)
    ok("beats: too many -> error", any("too many beats" in x for x in e))
    e, w = cue.validate_beats([{"start": 0, "action": "a"}, {"start": 5.2, "action": "b"}], 5.17, 2)
    ok("beats: past end -> error", any("past the clip end" in x for x in e))
    sch = cue.schema_for(cue.H3_FIELDS_BASE, True, True)
    ok("schema: key order", list(sch["properties"]) == ["subjects", "beats"] + cue.H3_FIELDS_BASE)
    ok("field names", cue.field_names("continuation (DaSiWa)", True, True) == ["next_action"] and cue.field_names("", False, False) == ["prompt"])


class FakeWriter:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, endpoint, model, system, user, images=None, seed=0, temperature=0.7, num_predict=1200,
                 on_chunk=None, timeout=180, schema=None, on_token=None):
        self.calls.append({"system": system, "user": user, "schema": schema, "seed": seed})
        ans = self.answers.pop(0) if self.answers else self.answers_last
        self.answers_last = ans
        if on_chunk:
            on_chunk(ans[: len(ans) // 2])
            on_chunk(ans)
        return ans


def run_node(fake, **kw):
    cue.ollama_chat = fake
    node = cue.Cue()
    base = dict(intent="", notes="", target="MiniMax H3", mode="t2v", duration_seconds=5.0, dialogue_language="English",
                detail="short", writer="fake", endpoint="http://x", seed=1, lock=False, unique_id="t")
    base.update(kw)
    r = node.run(**base)
    return r, node._payload, fake


def test_h3_t2v_retry():
    bad = json.dumps({"subjects": [{"token": "detective", "description": "a man in his forties, shaved head, grey hoodie"}],
                      "beats": [{"start_seconds": 0, "action": "a"}, {"start_seconds": 1, "action": "b"}, {"start_seconds": 2, "action": "c"}, {"start_seconds": 3, "action": "d"}],
                      "integrated_multimodal_description": "[Shot 1] {detective} lights a cigarette. [Shot 2] At 00:05.100, the camera cuts to his face.",
                      "overall_soundscape": "Rain.", "non_diegetic_music": "None."})
    good = json.dumps({"subjects": [{"token": "detective", "description": "a man in his forties, shaved head, grey hoodie"}],
                       "beats": [{"start_seconds": 0, "action": "lights"}, {"start_seconds": 3, "action": "speaks"}],
                       "integrated_multimodal_description": "[Shot 1] {detective} lights a cigarette in the rain. He (S1) says: \"Not tonight.\" The camera holds static.",
                       "overall_soundscape": "Rain on pavement.", "non_diegetic_music": "A low cello note."})
    r, p, fake = run_node(FakeWriter([bad, good]), intent='a detective lights a cigarette and says "Not tonight."')
    ok("t2v: retried once", p["retries"] == 1 and p["valid"], str(p["errors"]))
    ok("t2v: retry message names the budget", "too many beats" in fake.calls[1]["user"] and "too many shots" in fake.calls[1]["user"])
    ok("t2v: schema sent", fake.calls[0]["schema"] and "beats" in fake.calls[0]["schema"]["properties"])
    ok("t2v: budget in user message", "at most 2 beats" in fake.calls[0]["user"])
    prompt = r["result"][0]
    ok("t2v: fields in order", prompt.index("integrated_multimodal_description:") < prompt.index("overall_soundscape:") < prompt.index("non_diegetic_music:"))
    ok("t2v: token substituted once", prompt.count("a man in his forties") == 1 and "{detective}" not in prompt)
    ok("t2v: tokens output keeps the token", "{detective}" in r["result"][1])
    ok("t2v: dialogue wrapped", "<d>[English] Not tonight.</d>" in prompt)
    ok("t2v: mode out", r["result"][4] == "T2VA" and abs(r["result"][3] - 124 / 24) < 1e-6)
    ok("t2v: info line", "1 shot" in p["info"] and "5.17 s" in p["info"])


def test_i2v_header_and_last():
    ans = json.dumps({"subjects": [], "beats": [{"start_seconds": 0, "action": "x"}],
                      "integrated_multimodal_description": "[Shot 1] A woman turns.", "overall_soundscape": "s", "non_diegetic_music": "m"})
    try:
        import numpy as np
    except ImportError:
        print("skip i2v (no numpy)")
        return
    img = np.zeros((1, 64, 64, 3), dtype="float32")
    fake = FakeWriter([ans])
    cue.ollama_chat = fake
    cue.ollama_chat = lambda *a, **k: ("a woman stands in an alley." if k.get("num_predict") == 300 else fake(*a, **k))
    r, p, _ = run_node(fake, mode="i2v", intent="she turns", image=img)
    ok("i2v: header line", r["result"][0].startswith("For the target video, at 0.00 seconds"))
    r, p, _ = run_node(FakeWriter([ans]), mode="last", intent="she turns", image_last=img, duration_seconds=3)
    ok("last: header", "aligns with the 3.04-second mark" in r["result"][0])


def test_krea_and_continuation():
    ans = json.dumps({"subjects": [{"token": "superman", "description": "a muscular man in a blue suit and red cape"}],
                      "prompt": "{superman} dives through smoke above a burning street, a wide shot, harsh noon light. — 60 words —"})
    r, p, fake = run_node(FakeWriter([ans]), target="Krea 2", intent="superman saving people", seed=4)
    ok("krea: valid, no beats", p["valid"] and "beats" not in fake.calls[0]["schema"]["properties"])
    ok("krea: substituted + meta stripped", r["result"][0].startswith("a muscular man in a blue suit") and "60 words" not in r["result"][0])
    r, p, fake = run_node(FakeWriter([ans]), target="Krea 2", intent="superman saving people", seed=4,
                          subjects_override="superman = a tall woman with red hair")
    ok("krea: override announced + applied", "a tall woman with red hair" in fake.calls[0]["system"] and r["result"][0].startswith("a tall woman with red hair"))
    cont = json.dumps({"beats": [{"start_seconds": 0, "action": "draws"}], "next_action": "[Shot 1] He draws his gun and steps back."})
    r, p, fake = run_node(FakeWriter([cont]), mode="continuation (DaSiWa)", intent="he draws his gun", duration_seconds=4)
    ok("dasiwa: next action only, shot marker stripped", r["result"][0] == "He draws his gun and steps back." and p["valid"])
    reel = json.dumps({"beats": [{"start_seconds": 0, "action": "draws"}], "integrated_multimodal_description": "[Shot 1] He draws.", "overall_soundscape": "s", "non_diegetic_music": "m"})
    r, p, fake = run_node(FakeWriter([reel]), mode="continuation (Reel)", intent="he draws his gun", duration_seconds=4)
    ok("reel: window + seam policy prepended", "continuation window" in r["result"][0] and "Continue the same uninterrupted shot" in r["result"][0])


def test_text_fallback():
    text = "subjects:\n{man} = a tall man\n\nprompt:\nintegrated_multimodal_description: [Shot 1] {man} walks.\n\noverall_soundscape: s\n\nnon_diegetic_music: m"
    r, p, _ = run_node(FakeWriter([text]), intent="a man walks")
    ok("fallback: plain text still parsed", p["valid"] and r["result"][0].startswith("integrated_multimodal_description: [Shot 1] a tall man walks."), str(p["errors"]))


def test_no_fades():
    bad = json.dumps({"beats": [{"start_seconds": 0, "action": "turns"}], "integrated_multimodal_description": "[Shot 1] She turns. At 00:04.800, the frame cuts to black.", "overall_soundscape": "s", "non_diegetic_music": "m"})
    good = bad.replace(" At 00:04.800, the frame cuts to black.", " She keeps turning.")
    r, p, fake = run_node(FakeWriter([bad, good]), intent="she turns")
    ok("fade: first answer rejected, retry clean", p["valid"] and p["retries"] == 1 and "cuts to black" not in r["result"][0], str(p["errors"]))
    ok("fade: error names the rule", "no fades" in fake.calls[1]["user"])


def test_repairs_17_1():
    ok("ts: loose shapes", cue.normalize_timestamps("At 0.500 a. At 2.5 s b. At 04:591 c. At 02:75 d. At 00:04.500 e. at 0.920 seconds f") ==
       "At 00:00.500 a. At 00:02.500 b. At 00:04.591 c. At 00:02.750 d. At 00:04.500 e. at 0.920 seconds f")
    ok("fade: lighting phrase allowed", not cue._FADE_RE.search("the light fades to amber") and cue._FADE_RE.search("it fades to black"))
    ok("d-tags: untagged normalised, quotes stripped", cue.normalize_d_tags('<d>Go.</d> <d>[English] "Stay."</d>', "English") == "<d>[English] Go.</d> <d>[English] Stay.</d>")
    ok("d-tags: never nested", cue.fix_dialogue('He says <d>Not tonight.</d>', ["Not tonight."], "English").count("<d>") == 1)
    ok("speaker: pronoun", cue.guess_speaker('she turns and whispers "go"', "go") == "She")
    ok("speaker: noun from the intent", cue.guess_speaker('a detective lights a cigarette and says "Not tonight."', "Not tonight.") == "The detective")
    ok("speaker: word before verb", cue.guess_speaker('the bartender says "no"', "no") == "The bartender")
    body, n = cue.append_dialogue("[Shot 1] She turns", 'she turns and whispers "go"', ["go"], "Turkish")
    ok("append: speech added once", n == 1 and body.endswith("She (S1) says: <d>[Turkish] go</d>") and cue.append_dialogue(body, "x", ["go"], "Turkish")[1] == 0)
    ok("palette: no person axis for a place", "subject person" not in cue.draw_palette(3, "a rainy street") and "subject person" in cue.draw_palette(3, "a woman in the rain"))
    ok("palette: intent changes the draw", cue.draw_palette(3, "a rainy street") != cue.draw_palette(3, "a desert road"))
    # dropped dialogue through the node: both answers lack the line -> appended after the retry
    ans = json.dumps({"subjects": [], "beats": [{"start_seconds": 0, "action": "x"}], "integrated_multimodal_description": "[Shot 1] A detective lights a cigarette.", "overall_soundscape": "s", "non_diegetic_music": "m"})
    r, p, _ = run_node(FakeWriter([ans, ans]), intent='a detective lights a cigarette and says "Not tonight."', seed=9)  # seed 1 is cached by an earlier test
    ok("node: dropped line appended as speech", p["valid"] and "The detective (S1) says: <d>[English] Not tonight.</d>" in r["result"][0] and any("appended" in w for w in p["warnings"]), str(p["errors"]) + str(p["warnings"]))
    # long needs words
    short_long = json.dumps({"subjects": [], "beats": [{"start_seconds": 0, "action": "x"}], "integrated_multimodal_description": "[Shot 1] A man walks.", "overall_soundscape": "s", "non_diegetic_music": "m"})
    r, p, fake = run_node(FakeWriter([short_long, short_long]), intent="a man walks", detail="long")
    ok("long: too short is an error that is retried", p["retries"] == 1 and any("too short" in e for e in p["errors"]) and "too short" in fake.calls[1]["user"])


if __name__ == "__main__":
    for t in (test_parse_json, test_preview, test_budgets, test_h3_t2v_retry, test_i2v_header_and_last, test_krea_and_continuation, test_text_fallback, test_no_fades, test_repairs_17_1):
        t()
    failed = [n for n, c in RESULTS if not c]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed" + (f", FAILED: {failed}" if failed else ""))
    sys.exit(1 if failed else 0)
