"""Offline tests for Prompt Deck: line mode unchanged, block mode, vars.  python3 tests/test_prompt_deck.py"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import prompt_deck as pd  # noqa: E402

RESULTS = []


def ok(name, cond, note=""):
    RESULTS.append((name, bool(cond)))
    print(("ok   " if cond else "FAIL ") + name + (f"  ({note})" if note and not cond else ""))


def write(text):
    d = tempfile.mkdtemp()
    p = os.path.join(d, "deck.txt")
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    return p


def run(path, idx=0, **kw):
    base = dict(file_path=path, line_index=idx, pre_text="", after_text="", search="", replace="",
                cut_to="", cut_from="", rating_filter="all", disabled_lines="", resolved="", vars="")
    base.update(kw)
    return pd.PromptDeck().read(**base)


def test_line_mode_unchanged():
    p = write("# comment\n1. a cat, cinematic\n\n2) a dog\nplain line {not_a_var}\n")
    r = run(p, 0)
    ok("line: numbering stripped", r["result"][0] == "a cat, cinematic")
    ok("line: slug from text", r["result"][1] == "a_cat_cinematic")
    ok("line: mode reported", r["ui"]["deck_state"][0]["mode"] == "line")
    r = run(p, 1, cut_from="cinematic")
    ok("line: cuts still work", run(p, 0, cut_from=", cinematic")["result"][0] == "a cat")
    r = run(p, 2)
    ok("line: braces untouched when the vars box is empty", r["result"][0] == "plain line {not_a_var}" and r["ui"]["deck_state"][0]["unfilled"] == [])
    r = run(p, 2, vars="x = 1")
    ok("line: with vars set, an unknown token is reported", r["result"][0] == "plain line {not_a_var}" and r["ui"]["deck_state"][0]["unfilled"] == ["not_a_var"])
    entries = pd._load_entries(p)
    ok("line: entries one per line, blanks flagged", [e["blank"] for e in entries] == [True, False, True, False, False] and "name" not in entries[1])


BLOCK = """deck notes, ignored
## rooftop_istanbul
subject_definitions:
<Subject 1> is the {look|woman} in <Video 1>.

summary:
# a comment inside the block
She walks to the edge.

non_diegetic_music:
none
## neon_street
integrated_multimodal_description: [Shot 1] {look} walks under neon. {weather}

overall_soundscape: rain
"""


def test_block_mode():
    p = write(BLOCK)
    entries = pd._load_entries(p)
    ok("block: two entries named", [e["name"] for e in entries] == ["rooftop_istanbul", "neon_street"])
    ok("block: verbatim inner blank lines, comments dropped",
       entries[0]["raw"] == "subject_definitions:\n<Subject 1> is the {look|woman} in <Video 1>.\n\nsummary:\nShe walks to the edge.\n\nnon_diegetic_music:\nnone")
    r = run(p, 0)
    ok("block: default fills the var", "<Subject 1> is the woman in <Video 1>." in r["result"][0])
    ok("block: slug from the header", r["result"][1] == "rooftop_istanbul")
    ok("block: mode reported", r["ui"]["deck_state"][0]["mode"] == "block")
    r = run(p, 0, vars="look = short blonde woman wearing a black dress\n# comment")
    ok("block: var beats default", "is the short blonde woman wearing a black dress in" in r["result"][0])
    r = run(p, 1)
    ok("block: unfilled vars stay literal + warning", "{weather}" in r["result"][0] and r["ui"]["deck_state"][0]["unfilled"] == ["look", "weather"] and "{look}" in r["result"][0])
    r = run(p, 0, cut_from="non_diegetic_music:")
    ok("block: cut_from drops a section", r["result"][0].rstrip().endswith("She walks to the edge.") and "non_diegetic" not in r["result"][0])
    ok("block: no numbering strip", run(write("## one\n1. keep the number\n"), 0)["result"][0] == "1. keep the number")
    # ratings keyed by name survive a text edit
    pd._save_rating(p, "rooftop_istanbul", "good")
    with open(p, "a", encoding="utf-8") as f:
        f.write("\n")
    p2 = p
    with open(p2, "w", encoding="utf-8") as f:
        f.write(BLOCK.replace("She walks to the edge.", "She runs to the edge."))
    r = run(p2, 0, rating_filter="good only")
    ok("block: rating by name survives a text edit", r["result"][1] == "rooftop_istanbul" and r["ui"]["deck_state"][0]["runnable_total"] == 1)
    ok("block: entry key is the name", pd._entry_key(entries[0]) == "rooftop_istanbul")


if __name__ == "__main__":
    test_line_mode_unchanged()
    test_block_mode()
    failed = [n for n, c in RESULTS if not c]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed" + (f", FAILED: {failed}" if failed else ""))
    sys.exit(1 if failed else 0)
