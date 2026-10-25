# Windows runbook (for the Claude session on the 5090 box)

Machine: `C:\Users\omerv\Documents\ComfyUI`, RTX 5090 32 GB, Ollama installed. This is the
maintainer protocol for setting up and testing PromptDeck's **Cue** and **Reel** nodes there.
`WINDOWS_SETUP.md` is the user install guide; this file is for the test session. Written
2026-10-24, before either node exists.

## 0. Two gates

| phrase from Omer | allows |
| --- | --- |
| `GREEN LANTERNS MIGHT` | building the nodes (Mac session) |
| `LANTERNS MIGHT` | starting test generations on this box |

Until the test phrase arrives: **setup only, nothing queued.** Omer starts the overnight batch
deliberately; the first queued prompt is his call. Setup is §1 in full. The driver and judge (§2,
§4) are built with the nodes and only *configured* here.

## 1. Setup (allowed any time)

1. `git pull` in `custom_nodes\ComfyUI-PromptDeck`, restart the ComfyUI server (not a browser
   refresh). Confirm the nodes exist via `GET /object_info`: `PromptDeckCue`,
   `PromptDeckReelGuide`, `PromptDeckReelSplice`. Node-import log clean.
2. ComfyUI commit past `e01fb4c` (native `MiniMaxH3AddGuide`). Record the commit in the run log.
3. Ollama models, record `ollama list` in the log:
   - writers: `huihui_ai/qwen3-vl-abliterated:4b-instruct`, `huihui_ai/qwen3-vl-abliterated:2b-instruct`
   - judge: the largest abliterated Qwen3-VL that fits with ComfyUI unloaded (try the 30B-A3B
     and 32B instruct tags at Q4_K_M; note which tag exists and its size). If none fits, the 9B.
4. Confirm `http://127.0.0.1:11434/api/tags` answers from this machine, and that one text-only
   request to the 4B returns (no image, no generation).
5. Inputs in `ComfyUI\input\promptdeck_tests\`:
   - `intents_video.txt`: about 20 lines, mixed subjects (not only women), at least two violent
     beats, two undress-and-cut-to-black beats, three with dialogue in quotes, one with Turkish
     dialogue, one long brief, one single word.
   - `intents_image.txt`: about 20 lines for Krea 2, same spread.
   - `refs\<character>\1.png 2.png 3.png` for three characters; `starts\*.png` start frames;
     for Reel, three source clips as latents or videos of 5 s.
6. Node and input names as built (for the batch files' `set` entries): Cue = class `PromptDeckCue`,
   inputs `intent`, `notes`, `target` ("MiniMax H3" / "Krea 2" / "Generic"), `mode` (`t2v`, `i2v`,
   `first+last`, `last`, `ref2v`, `continuation (DaSiWa)`, `continuation (Reel)`),
   `duration_seconds`, `dialogue_language`, `detail` (`auto/short/medium/long`), `writer`,
   `endpoint`, `seed`, `lock`, optional `image`, `image_last`, `ref_1..9`, `label_1..9`,
   `subjects_override`. Reel Guide = `PromptDeckReelGuide`, inputs `op`, `tail_frames` and
   `head_frames` (**strings** "5".."73"), `new_seconds`, `width`, `height`, `save`, `session`,
   optional `previous`, `next`. Reel Splice = `PromptDeckReelSplice`.
7. Test workflows, saved in **API format** into `custom_nodes\ComfyUI-PromptDeck\tests\workflows\`.
   "API format" is ComfyUI's own export (Workflow menu → Export (API), or enable dev mode and
   use "Save (API Format)"): a JSON keyed by node id that the server accepts on `POST /prompt`.
   The normal saved workflow is the canvas and cannot be queued. Files:
   `cue_h3_t2v.json`, `cue_h3_i2v.json`, `cue_h3_ref2v.json`, `cue_krea2.json`,
   `reel_extend.json`, `reel_bridge.json`, `reel_prequel.json`, `reel_refine.json`.
   **The test graphs use the native H3 nodes, not the Director**: Cue `prompt` →
   `MiniMaxH3ImageToVideo` / `MiniMaxH3ReferenceToVideo` prompt, with `Load Image` nodes titled
   `Start Frame`, `Ref 1`, `Ref 2`, `Ref 3` feeding both Cue and the native node. The driver swaps
   those images per item; the Director loads media from its own timeline, which a script cannot
   swap per item. Omer's daily graph keeps the Director (`external_prompt_overwrite`). Node titles
   are what the batch files' `set` entries refer to; `--dry-run` lists every mismatch. End each
   graph with Save Video (or Keeper in `all` mode for stills).
8. Dry checks, no queueing: each workflow JSON validates against `/object_info` (every node
   class and input name exists); the batch files in `tests/batches/` point at existing paths.

## 2. The driver

`tests/overnight.py`, stdlib only, built with the nodes:

- reads one batch file (`tests/batches/<name>.json`): workflow, intent file, modes, writer,
  detail, seeds, refs, duration;
- substitutes values into the API JSON, `POST /prompt`, polls `/history/<id>`, copies outputs to
  `output/promptdeck_tests/<batch>/<item>/`, writes `run.json` there (prompt text, validator
  result and retry count, writer tag, seed, timings);
- resumable: skips items whose `run.json` exists; `--all` continues to the next batch;
- after a batch: `POST /free` with `{"unload_models": true, "free_memory": true}` so the judge
  has the card, then runs the judge (§4).

## 3. Batches (only after `LANTERNS MIGHT`)

| batch | size | question |
| --- | --- | --- |
| `cue-format` | 20 intents × 3 H3 modes × 2 writers, prompts only, no video | validator pass rate first try / after retry; seconds per prompt |
| `reel-anchors` | 3 sources × {extend, bridge, prequel} × head {5, 22, 39} × 2 seeds | seam quality, identity, saturation climb |
| `cue-video` | best writer, 20 intents × {i2v, ref2v}, 5 s | does the video do the intent; identity vs refs; Turkish line |
| `reel-refine` | best anchor setting, with / without the 10-step 0.15 refine | does the refine help the seam |
| `cue-image` | 20 intents × Krea 2 | prompt quality, by judge and by eye |

Run in that order: the cheap prompt-only batch first, the experiments next, images last. One
batch per invocation unless `--all`.

## 4. Judge

The bigger vision model scores each output after its batch, with ComfyUI unloaded. It sorts and
tie-breaks; it is not the truth. Omer reviews in the morning.

- Inputs per item: 8 frames sampled evenly across the clip, plus 4 frames around each seam for
  Reel items; the intent; the prompt; the reference pictures when there are any.
- Output per item, `judge.json`: `intent_match`, `prompt_match`, `identity_match`, `seam`
  (Reel only), `artifacts` (burn, saturation climb, stalls), each 1–5, plus one sentence.
- Collected into `results.csv` per batch and an `index.html` contact sheet (thumbnails, scores,
  prompt, path) for the morning review.
- The judge prompt lives in `tests/judge_prompt.md` and stays identical across batches so runs
  compare.

## 5. Report back

One message to Omer (or the Mac session): pass rates, the top and bottom three per batch with
paths, anything that crashed, the Ollama tags actually used, the ComfyUI commit. Numbers and
paths only; the conclusions are Omer's.

## 6. What happened (2026-10-25)

Runs 1-4 were executed on the box; results, verdicts and the open items are in `docs/WINDOWS_HANDOFF.md` (repo) and `output/promptdeck_tests/REPORT.md` (box). Read the handoff before touching the tests.

## 7. 1.7 re-test (next on the box)

Pull, restart ComfyUI, then re-run `cue-format`, `cue-format-i2v`, `cue-format-ref2v` and `cue-image-terse` with the 4B, and the same with the 32B writer (`--set` the writer or a copy of the batch). Compare `v_valid`, `v_retries` and beat counts against the 1.6 results. The JSON protocol needs Ollama >= 0.5; the box's version is in `ollama -v`.

## 8. 1.8: writer bake-off and the palette (next on the box)

1. Pull, restart. Set `writer` to a text model and keep `vision_writer` on the 4B VL.
2. Pull every candidate now (Omer has the space; unused ones get deleted later with `ollama rm`).
   If a tag does not exist, `ollama pull` says so: search `ollama.com/huihui_ai` for the current
   spelling and note it in the handoff.

   ```
   ollama pull huihui_ai/qwen3-abliterated:8b
   ollama pull huihui_ai/qwen3-abliterated:30b-a3b
   ollama pull huihui_ai/mistral-small-abliterated:24b
   ollama pull huihui_ai/gemma3-abliterated:27b
   ollama pull huihui_ai/qwen3-vl-abliterated:8b-instruct
   ```

   Run `cue-image-terse` with each text model as `writers` (prompts only),
   compare `v_valid`, words, time per prompt, and read ten prompts each by eye for register and variety.
   Pick the daily writer from that; the 32B stays for `long` if nothing beats it.
3. Palette: `python tools/make_palette.py --writer <best text model> --per-round 40 --rounds 25`
   overnight (about 1000 new lines per axis; saves after every round, safe to interrupt). Commit
   `templates/palette.md` afterwards.
