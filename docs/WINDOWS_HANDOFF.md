# Windows box handoff — PromptDeck tests, 2026-10-25

For the next session (any machine). Everything below happened on Omer's 5090 box in one Claude session;
the repo working tree is **uncommitted** (`git status` lists it). Results live on the box in
`C:\Users\omerv\Documents\ComfyUI\output\promptdeck_tests\` — `REPORT.md` (numbers + verdicts),
`REEL_VS_DASIWA.md` (mechanism diff), `setup_log.md` (timeline), per-batch `results.csv` / `index.html`.
Box facts and gotchas (Comfy restart, Ollama, pip) are in the Claude memory notes `comfy-update-runbook`,
`promptdeck-overnight-lessons`, `plan-tests-fully-before-running`.

## What was run

| run | when | what | result |
|---|---|---|---|
| 1 | 03:07–06:26 | all 5 runbook batches (211 items) + VLM judge | 211/211 generated; judge numbers unreliable for video |
| 2 | 07:05–07:39 | prompt-only re-tests after fixing the test design; Reel re-judged in seam mode | i2v 1.6 → 5.0 once intents fit the frame; ref2v labels unify refs |
| 3 | 14:21–17:18 | Reel with Cue fed the edge frame; cue-video-i2v / -ref2v with fixed intents (64 clips) | 64/64 generated |
| 4 (A/B) | 17:20–17:41 | Reel extend ×4 with exact audio / window sentence / Director sampler, judged by eye + trace | **Reel seam clean**; audible seam = silent source |
| calib | 17:55 | 12 terse intents through Cue for Krea 2, prompts only | tone calibrated; soul/variety not (see below) |

## Verdicts

- **Reel**: the latent join is clean (sheets + `tests/seam_metric.py`); mechanism identical to DaSiWa's.
  Not tested inside the Director graph. Reference clips need audible audio.
- **Cue video (H3)**: structurally valid prompts, validator holds, labels work. Problem: pacing — 3–4 beats
  into a 5 s clip (one shot, ≤2 beats would fit). Not fixed.
- **Cue image (Krea 2)**: tone calibrated (`sexy` ≠ porn, `nude` = art nude). Problems: one default
  protagonist/setting for every subjectless intent; garbled token joins ("The a young woman"); writer
  meta-lines leaking ("— 120 words —"). Fixes in progress at the end of the session — see "Last changes".
- **VLM judge (32B)**: fine for sorting prompts, not a verdict on video or identity. Seams are judged with
  `tests/seam_sheet.py` + `tests/seam_metric.py`.

## Repo changes (uncommitted)

- `tests/overnight.py`: `--no-judge`; `--judge-only` frees Comfy once; dry-run accepts autogrow dotted ids
  and subfolder file paths; `prompt_text` falls back to `validator.prompt`; judge `kind: seam` (frames around
  cuts only); `gather_frames` skips copied source clips; judge payload `num_ctx` 32768 + `keep_alive` 30m.
- `tests/judge_prompt.md`: seam-mode rule.
- `tests/batches/*`: judge tag `32b-instruct-q4_K_M`; Reel batches trimmed to one source + seam judge; new
  batches `cue-format-i2v`, `cue-format-ref2v`, `cue-video-i2v`, `cue-video-ref2v`, `reel-ab*`, `cue-image-terse`.
- `tests/workflows/`: generators `make_cue_h3.py`, `make_cue_krea2.py`, `make_reel.py`, `make_reel_ab.py` and
  their API-format graphs (portrait 768×1344; ref2v `label_1..3 = char1`; Reel feeds Cue the edge frame).
- `tests/prepare_sources.py` (conform a clip: 124 f @ 24, canvas, audio exactly tokens×800 samples, pcm .mov),
  `tests/seam_sheet.py`, `tests/seam_metric.py`, `tests/face_id.py` (unused; needs numpy<2 insightface).
- `cue.py`: Reel mode prepends the DaSiWa-style window-context sentence (system-side).
- Inputs on the box: `input/promptdeck_tests/` (intents_*.txt incl. `intents_i2v`, `intents_ref2v`,
  `intents_terse`; refs; starts; sources incl. `char1_exact_5s.mov`).

## Open items (for the Mac / product)

Status 2026-10-25 evening (Mac, v1.7): items 1, 4 and 6 are done (beat/shot budget with JSON beats planning; cache salted with CUE_VERSION; `valid`/`errors`/`retries` in the ui dict). Item 3 is covered by the article-safe substitution plus description cleaning. Re-run `cue-format*` on the box to compare 1.7 against the 1.6 numbers.

1. Cue pacing: beats-per-duration rule in the H3 template (≤5 s → one shot, ≤2 beats, no cutaways).
2. Cue image variety: seeded palette + "no person unless implied" + content rule by register (in progress).
3. `substitute_tokens`: drop the preceding article / require a noun phrase for token descriptions.
4. Cue cache (`cue_cache.json`): key has no code-version salt; system-side framing is frozen into cached
   prompts. Salt the key or apply framing after `cache_get`.
5. Reel `plan()`: clamp anchors to the clip instead of raising.
6. Cue validator errors are not exposed in the ui dict (`v_*` can't say which rule failed).
7. Driver: strip UTF-8 BOM in intent files.
8. Reel inside the Director graph: untested.

## Last changes (end of session, 2026-10-25 ~18:00)

See the bottom of `setup_log.md` on the box for what landed and the before/after of the 12 terse intents.

### Last changes, detail (2026-10-25 18:00–18:35)
- `cue.py`: `draw_palette`, article-safe `substitute_tokens` + stray-brace strip, `strip_meta_lines`, noun-phrase
  token instruction, temperature 0.9 for image targets, `{palette}` fill. **Encoding note:** the file was once
  double-encoded by a PowerShell edit and repaired; verify `▾ ↻ —` glyphs render before trusting diffs.
- `templates/krea_2.md`: register rule, no-person-unless-implied, anti-house-style, group irregularity, `{palette}`.
- `templates/minimax_h3.md`: system prompt now opens with "describe everything that moves, continuously";
  `detail` = coverage (short/medium/long); rules for per-element motion, ≤2 actions per 5 s with
  onset/development/end, group irregularity; register rule.
- `templates/palette.md`: seeded variety palette (subagent-written) — if absent, `draw_palette` returns "".
- New batches: `cue-image-terse`, `cue-format-motion`, `cue-format-motion2`; inputs `intents_terse.txt`,
  `intents_motion.txt`.
- Next for Cue (product): duration-from-beats planning (or beat trimming) in Cue, timestamp-format validation,
  surface validator errors, 32B as the default `long` writer, salt the prompt cache with a code version.
- `templates/palette.md` exists (written by the session itself after the subagent was stopped). `cue.py` also
  guards against writers restating a token's description inline, and strips trailing "Note:" paragraphs.
- Final Krea 2 numbers and the remaining 4B weaknesses: `REPORT.md`, last section. Comfy left running.

## 1.7 re-test on the box (2026-10-25 21:25–22:47)
- Ran runbook §7 against b9ba9d6: 8 prompt-only batches (4 with 4B/2B/8B, the same 4 with the 32B, new
  `tests/batches/*-32b.json`), 446 prompts, results in `output/promptdeck_tests/v17/` (`COMPARE.txt`,
  `NOTES_17_RETEST.md` with per-batch notes, `compare_17.py`, `replay_errors.py`).
- Verdict and the 7 findings: `output/promptdeck_tests/REPORT.md`, section "1.7 re-test". Headline: ref2v
  fixed (10–14 % → 86–100 %), 32B is the `long` writer (95–100 % valid, 375–470 words).
- Fixed here: `tests/overnight.py` kept only scalar validator keys, so 1.7's `errors`/`warnings` lists never
  reached run.json — lists are now joined with " | ".
- Applied on the box after Ömer's note that fades/cuts to black are never wanted in a prompt: H3 template rule
  + `_FADE_RE` validator error in `validate_h3` (+ test), intents i06/i11 lose their "cuts to black" clause.
- Status 2026-10-25 late (Mac, 1.7.1): all five Mac items below are done (append repair for dropped dialogue with a speaker guessed from the intent; d-tag normalisation, no nesting; timestamp normaliser for `At` cut markers; palette mixes in the intent and drops the person axis for no-person intents; minimum words for `long`). `_FADE_RE` no longer matches lighting phrases like 'fades to amber'.
- Mac items (applied, were in priority order): speech field or append-repair for dropped dialogue; `fix_dialogue`
  must not nest an existing untagged `<d>`; timestamp normaliser (`0.500`, `02:75`, `04:591` → `00:SS.mmm`);
  `draw_palette` must mix in the intent and drop the person axes
  for no-person intents; minimum-words check for `long`.

## 1.8 on the box: runbook §8 done (2026-10-26 01:30–03:22)
- Pulled 3d6ec0e (1.8 → 1.8.2), Comfy restarted on it. Ömer's saved workflows loaded the Cue node with widget
  values one slot off (framing = model name, writer = null, seed = 0) after the new inputs — re-add the node once
  per workflow.
- Models: all five tags existed as spelled. `gemma3-abliterated:27b` is fp16 (55 GB) and the only alternative is
  q8_0 (30 GB), neither fits the 5090 with context; pulled `gemma3-abliterated:12b-q8_0` instead and it fails to
  load on Ollama 0.40.7 (`token_embd.weight` shape mismatch) — Gemma needs a newer Ollama. The 55 GB fp16 is
  still on disk.
- Bake-off `tests/batches/cue-image-terse-bakeoff.json` (13 terse intents × 6 writers, prompts only):
  `output/promptdeck_tests/REPORT.md` "1.8 writer bake-off". Pick: **mistral-small-abliterated:24b** as the daily
  text writer (13/13 valid, 133 words, 13 s, best variety); 32B stays for `long`; vision_writer the 4B VL.
- Cue bug (Mac): the 8B writers restate part of a subject's description right before its token and the
  substitution doubles it ("young young woman", "A middle-aged the man"); drop the overlap before substituting.
- Palette: `tools/make_palette.py --writer mistral-small-abliterated:24b --per-round 40 --rounds 25` ran 26 min,
  +6514 lines (535–795 per axis, no duplicates); committed in `templates/palette.md`. Worth a prune pass on the
  camera axis (mash-ups like "pallbearer's shoes reflecting streetlight downwards quick left").
- API graphs `tests/workflows/cue_*.json` carry the 1.8 required inputs `vision_writer` and `framing`.
