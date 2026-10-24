# H3 Reel — design plan (tabled 2026-10-19, not started)

Experimental node set for the PromptDeck repo: latent-level time editing for MiniMax H3 videos.
Extend, prequel, bridge, cut-and-fill, loop, takes, polish pass. Sits **downstream** of whatever builds
the conditioning (DaSiWa Director + Guide today, native nodes tomorrow). Director-optional, never
Director-dependent.

Status: design agreed 2026-10-14; facts corrected against verified source 2026-10-21/24 and a v1
contract added (§8). Nothing built. Go-word still pending. Testing runs in the overnight batches of
`docs/WINDOWS_RUNBOOK.md`, bundled with Cue (`docs/CUE_DESIGN.md`).

---

## 1. Why this exists

We analysed DaSiWa's `MiniMaxH3 Director` continuity (repo `darksidewalker/ComfyUI-DaSiWa-Nodes`, GPL-3).
Findings:

- Continuity is ~7,500 lines, but the actual mechanism is ~100 lines of tensor slicing, vendored (MIT)
  from `ttulttul/ComfyUI-Minimax-H3-Continuation`. The rest is timeline UI, LLM "Forge", and a
  filesystem checkpoint database.
- The vendored mechanism knows one move (DaSiWa has since stacked a bridge in `transition.py`, a
  delta segment store and an upscale-continuity pass on it, all GPL-3): pin the previous clip's
  **tail** at the **head** of a new window, generate
  forward, discard the overlap, append. Context is fixed to one of 5/22/39/56/73 frames and gets
  silently squeezed to 5 when you ask for 15 s (that is the "running man becomes walking man" bug:
  5 frames = 0.2 s of motion evidence).
- Native ComfyUI (commit `e01fb4c`+) ships **`MiniMaxH3AddGuide`**: anchor an image, a *multi-frame
  clip*, or audio at **any** frame index, negative counts from the end, chainable. The bridge/prequel
  primitive already exists upstream; nobody has wired it into a workflow.
- Forking the Director is a trap: 27k vibecoded lines + GPL. We take the mechanism, not the code.

## 2. The mechanism (what we rely on)

H3 latent facts:
- Pixel frames must satisfy `17k + 5`. 17 frames -> 5 video tokens, plus a 2-token stub for the first 5.
  `video_latent_t(n) = 2 if n <= 5 else ((n-5)//17)*5 + 2`. Cuts therefore snap to 0.708 s steps.
- Audio latent is 40 Hz: `audio_t(frame) = round(frame * 40/24)`. Both streams live in one
  `NestedTensor((video, audio))` under `latent["samples"]`. Video shape `[1, 24, T, H/16, W/16]`,
  audio `[1, 32, 2, Ta]`.
- **362 frames** (~15.08 s) is the trained range and DaSiWa's policy cap, not an attention cap
  (native `length` accepts more; quality beyond it unverified). Everything sampled in one go stays
  within it.
- Keyframe anchors live in `positive[0][1]["minimax_keyframes"]` as a list of
  `{"resolved_frame_index": int, "latent": video_tokens, "audio_latent": audio_tokens}`.
  Plain data; we can strip, replace, add.

Operations (all = "build a zero window, drop anchors into the conditioning, sample, splice"):

| Op | Anchors | Window | Splice |
| --- | --- | --- | --- |
| Extend | tail(prev) @ 0 | tail + new | drop tail, append new |
| Prequel | head(src) @ -N | new + head | drop head, prepend new |
| Bridge / fill | tail(left) @ 0, head(right) @ -N | tail + new + head | drop both, insert new |
| Loop close | tail(last) @ 0, head(first) @ -N | same as bridge | insert, film now loops |
| Cut | none | none | remove tokens on the 17-grid; add a 1 s fill to heal the hard splice |
| Polish | none (img2img) | chunked, 5 s + 1 s overlap | blend chunks, audio frozen by zero noise mask |

Budget: `tail + new + head <= 362`. Head and tail are **independent dials** (e.g. tail 73, head 22).

Open experiment #1: multi-frame *end* anchors. Native AddGuide allows it; model quality at a
22-frame end latent is untested. First thing to try on the 5090 before any UI exists.

## 3. Nodes (v1)

Universal types only: CONDITIONING and LATENT in, CONDITIONING and LATENT out.

**Reel Guide** (the brain, carries the strip UI)
- in: `positive` (from Director Guide or native), optional `reel` state, dials: `tail_frames`,
  `head_frames` (5/22/39/56/73 each), `new_seconds`, `op` (extend / prequel / bridge / cut / polish)
- step 1, sanitize: strip any existing keyframe that collides with our anchors (lets the Director stay
  in I2VA with the start image loaded for the whole film: segment 1 uses the image, later segments
  weld to the tail). Keep REF2VA refs and audio refs untouched.
- step 2, slice anchors directly from stored latents. No VAE round trip (native AddGuide re-encodes
  pixels; we already have tokens).
- step 3, allocate the zero window, return `positive`, `latent`, `reel_ctx`.
- out: `positive`, `latent` (window), `reel_ctx`

**Reel Splice** (dumb on purpose, replaces DaSiWa Append & Stage)
- in: `sampled` (sampler output), `reel_ctx`
- drops overlap tokens, concatenates on the 17-grid, validates the global audio boundary
- out: `cumulative_latent` -> VAE Decode (video) + VAE Decode Audio, plus `reel` state for storage

**Reel store** (Keeper pattern, applied to latents)
- per segment: N takes, `latent.safetensors` + `meta.json` (frames, canvas, prompt line, parent take)
- two-phase like DaSiWa (staged -> ready after export) is worth copying; it prevents half-runs from
  being selectable
- lives under ComfyUI `output/promptdeck_reel/<session>/`

**Strip UI** (on Reel Guide)
```
┌──────────┬──────────┬──────────┬ ─ ─ ─ ─ ─┐
│  [thumb] │  [thumb] │  [thumb] │  (ghost) │
│  0-5s    │  5-10s   │  10-15s  │  next    │
│ she sits │ she turns│ he enters│ they talk│
│   ×1     │   ×3     │   ×1     │          │
└──────────┴──────────┴──────────┴ ─ ─ ─ ─ ─┘
```
- tiles = segments; caption = the Prompt Deck line that made it; ×N = takes, click to cycle
- ghost tile = next Prompt Deck line, so you see what queue will do
- retake segment 2 -> tiles 3+ grey (built on a different take); bridge re-heals instead of staling
- click left of the first tile = prequel; click a seam = last-frame-left | first-frame-right compare
- Clear -> three empty squares; load video 1 and 3 -> bridge generates 2
- no toasts, minimal chrome (Keeper rules)

## 4. Workflow slot (user's current graph)

Prompt path: **Prompt Deck → Cue → Director (`external_prompt_overwrite`)**, Director continuity off.
Live path inside the user's "Dasiwa-MinimaxH3" subgraph, everything else bypassed:
`Director -> Director Guide -> SamplerCustomAdvanced -> Append & Stage -> VAE Decode x2 -> export`

We touch three wires:
```
Guide.positive ──► Reel Guide ──► guider
                   Reel Guide ──► sampler.latent
Sampler.output ──► Reel Splice ──► VAE Decode (replaces Append & Stage)
```
Rules:
- Director continuity **off** (Clear source). Not because of conflict (we sanitize anyway) but
  because with it on, the Guide loads their checkpoint and preps a window we then discard.
- Seam-policy text must be in the prompt *before* encoding. Cue's `continuation (Reel)` template
  writes it (CUE_DESIGN.md §7.1). Without Cue, use Prompt Deck **pre-text** (snippet to ship: "Continue the same uninterrupted shot. Preserve identity, clothing, lighting,
  environment. Match positions, instantaneous motion, camera and sound at the seam. Do not restart
  the action, repeat dialogue, cut, fade, freeze, loop, or swap identity.").
- Prompt Deck line N = segment N. Increment control walks the storyboard.

## 5. Later / answered questions

- **Polish pass + LoRAs**: pass takes a MODEL socket; plug the LoRA-loaded model (or a different
  stack: detail LoRA, character LoRA at higher weight). Short chunks are free in a polish pass because
  motion already exists; chunk size is a dial on the 17-grid. User reports 15 s chunks spill to RAM
  on the 5090 at their settings; 5 s is the starting default.
- **Character swap**: not a reel feature, a *retake mode*. Native `MiniMaxH3FunControlNetApply`
  (Alibaba Fun ControlNet Union v2.0, Sept 2026, free weights) takes mask + source_video +
  control_video (pose) and regenerates only inside the mask. Identity via REF2VA picture or LoRA.
  Reel retakes a segment with the Fun-patched model and bridges both seams. Needs the pruned int8
  patch (raw 13.5 GB + H3 INT8 20.7 GB won't fit 32 GB).
- **Motion-aware context**: measure token-to-token difference in the tail, auto-bump context when
  motion is high. Not v1.
- **Hero-frame identity anchor**: harvest a frame from segment 1 as a REF2VA ref for all later
  segments; short-term memory from the tail, long-term from the ref. Not v1.

## 6. Build order

1. CPU smoke: fake AV latents in the Mac sandbox (`ComfyUI-dev-sandbox`, `--cpu`, 8 GB cap). Prove
   window shapes, anchor placement, splice arithmetic, audio boundary. Sandbox ComfyUI must be
   pulled to >= `e01fb4c` for native AddGuide.
2. 5090 experiment: extend (known good) vs bridge vs prequel at head anchors of 5 / 22 / 39 frames,
   with and without the 10-step 0.15 refine. Runs as the `reel-anchors` and `reel-refine` batches in
   `docs/WINDOWS_RUNBOOK.md`, judged by the vision model and by Omer in the morning.
3. Reel Guide + Reel Splice, no UI, widgets only. Wire into the user's subgraph.
4. Store + strip UI (Keeper patterns from `web/js/keeper.js`).
5. Cut / fill / loop. Then polish. Then swap-as-retake.

## 7. v1 contract (what gets coded first)

Types: `CONDITIONING`, `LATENT`, plus one dict type `PROMPTDECK_REEL_CTX`.

**Reel Guide**
- in: `positive` CONDITIONING (Director Guide or native); `previous` LATENT optional (the film so
  far); `next` LATENT optional (bridge / loop: the clip to the right); `op` combo
  `extend / prequel / bridge / loop / cut / polish`; `tail_frames` and `head_frames` combos
  `5 / 22 / 39 / 56 / 73`; `new_seconds` FLOAT (snapped to 17-frame steps); `width`, `height` INT
  (used only when there is no `previous`); `save` BOOLEAN.
- does: strip colliding `minimax_keyframes` from `positive[0][1]` (REF2VA refs and audio refs
  untouched); slice anchors straight from stored tokens (video on the 17-grid, audio at
  `round(f*40/24)` on the **global** timeline); allocate a zero window of `tail + new + head`
  frames (≤ 362); add the tail anchor at frame 0 and the head anchor at `-head_frames`.
- out: `positive` CONDITIONING; `latent` LATENT (the window); `reel_ctx` dict:
  `{op, tail_frames, head_frames, new_frames, window_frames, tail_video_t, tail_audio_t,
  head_video_t, head_audio_t, previous_frames, next_frames, session, segment}`.

**Reel Splice**
- in: `sampled` LATENT; `reel_ctx`; `previous` LATENT optional; `next` LATENT optional.
- does: drop the tail and head overlap tokens; concatenate on the 17-grid; recompute the audio
  boundary from the global frame count; assert `frames = 17k + 5` and the audio length matches.
- out: `latent` LATENT (cumulative); `segment` LATENT (new tokens only); `frames` INT.

**Store, v1 minimal:** `output/promptdeck_reel/<session>/<segment>/<take>/latent.safetensors` +
`meta.json` (`frames`, `width`, `height`, `prompt`, `op`, `parent`), written by Splice when `save`
is on; staged → ready once fully written. The strip UI (step 4) reads it later.

**Experimental, settled by the overnight batch:** quality of multi-frame *end* anchors (bridge,
prequel, loop) at 5 / 22 / 39 frames, and whether the 10-step 0.15 refine of the window before
splicing helps the seam. Extend is known good.

## 8. References

- DaSiWa pack: https://github.com/darksidewalker/ComfyUI-DaSiWa-Nodes (docs/h3_continuity.md,
  nodes/h3_continuity/core.py, nodes/h3_continuity/vendor/continuation_nodes.py)
- Vendored mechanism (MIT): https://github.com/ttulttul/ComfyUI-Minimax-H3-Continuation
- Native nodes: ComfyUI `comfy_extras/nodes_minimax_h3.py` (AddGuide, ReferenceToVideo,
  FunControlNetApply, `align_frame_count`, `video_latent_t`)
- Fun ControlNet: https://docs.comfy.org/tutorials/video/minimax/minimax-h3-fun-controlnet
- H3 prompt guides: https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/docs/

## 9. Implementation notes (v1 built 2026-10-24, `reel.py`, CPU-tested only)

- `PromptDeckReelGuide` / `PromptDeckReelSplice`, ctx type `PROMPTDECK_REEL_CTX` (a dict, `version: 1`).
- **Gap snapping is per op.** extend / prequel: multiples of 17 (`snap_extension`). bridge / loop:
  `17m - 5` frames (12, 29, 46 …, `snap_gap`), because tail + gap + head must itself be 17k+5. A
  first segment (extend with no `previous`) snaps to 17k+5 (`snap_first`) and adds no anchors.
- **prequel and loop take their head anchor from `previous`** (the film's own start); `next` is
  only for bridge. Loop keeps the window's regenerated head in the film (previous + gap alone is
  never on-grid); `reel_ctx["loop_point_frame"] = P + gap` tells the exporter where to cut.
- Anchors are built exactly like native `MiniMaxH3AddGuide` entries
  (`{resolved_frame_index, latent, audio_latent}`, token slices, sorted by index), including its
  audio clamp `floor(window_audio - 5/3 * frame)`. Existing keyframes are kept only when they fit
  the window and do not overlap an anchor region; `minimax_refs` is never touched.
- Audio counts are global-boundary differences (ttulttul rule). Films given to Guide/Splice must
  have audio exactly `round(frames*40/24)`; Splice forces the result back onto that boundary,
  trimming or repeating at most one 25 ms tick at the very end when a foreign clip (bridge) was
  joined. The `segment` output (new tokens only) is deliberately not a standalone-valid latent.
- Store: `output/promptdeck_reel/<session>/<op>@<previous_frames>/take_NNN/latent.safetensors`
  (`video`, `audio`, `segment_video`, `segment_audio`) + `meta.json`; both written as `.staged`
  then renamed. `cut` and `polish` raise NotImplementedError until the strip UI.
- Tests: `tests/test_reel_cpu.py` (run with
  `/Users/omer/Documents/Projects/ComfyUI-dev-sandbox/.venv/bin/python tests/test_reel_cpu.py`;
  the sandbox was pulled to `7cebca5` and given `comfy-aimdo`/`comfy-kitchen`). They prove the
  grid and audio arithmetic, anchor slices, splice layouts and the AddGuide format match. Seam
  quality, end-anchor behaviour and real sampler compatibility are GPU-only and still open.
