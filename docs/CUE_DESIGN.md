# Cue — design (2026-10-24, not started)

Third PromptDeck node, working name **Cue**. An intent box ("undressing", "fighting",
"superman saving people", or a long brief), optional pictures, a target-model dropdown. A small
local vision model writes the finished prompt in the format that target model actually wants.

Status: go-word given 2026-10-24 (late). **Built** the same day: `cue.py`, `web/js/cue.js`,
`templates/`. Tested on the MacBook Air against the 2B writer (t2v, i2v, Krea 2 all produce
well-formed output); GPU and 4B tests wait for the Windows box (`docs/WINDOWS_RUNBOOK.md`).
Deviations from this design are marked **as built**.

---

## 1. Why this exists

- **Local MiniMax H3-Base does not want a normal prompt.** It was trained on the output of
  MiniMax's hosted rewriter (H3-Context-IR), which is *not* open-sourced. MiniMax published two
  writing guides and an agent skill describing the exact format instead. Nobody ships a local
  rewriter; DaSiWa's Director "Forge" is the closest. Cue is that rewriter, for every H3 mode.
- **Hosted LLM/VLM APIs refuse the content** (violence, undressing that cuts to black). The
  writer must be a local abliterated model.
- **The format is where the quality is**, not the LLM. H3 wants six labelled sections and a
  timeline; Krea 2 wants long front-loaded sentences; SDXL wants tags. Templates are text files,
  so a new target model is a new file, never new code.
- **It composes with Prompt Deck.** Feed a text file of actions as the intent, queue 20, get 20
  cutscene prompts. Actions × characters is how a game's cutscene list looks.
- Reference images are *not* described away: identity still comes from the target model's own
  reference inputs. Cue does nothing about drift on chained H3 segments; that is the Reel plan.

## 2. Non-goals

- Nothing project-specific in code. No "noir" or "Tulip" preset. World and house rules live in
  a per-workflow notes box. The next game may not be noir.
- No hosted API dependency. (An OpenAI-compatible endpoint field is a later nicety.)
- No Python dependencies. Stdlib HTTP to Ollama, one JS file, like the other two nodes.

## 3. The node

Under **PromptDeck → Cue**. Output node (runs standalone, like Prompt Deck), so prompts can be
iterated without loading any image or video model.

### 3.1 Inputs

| widget / socket | type | notes |
| --- | --- | --- |
| `intent` | multiline STRING, also a socket | the ask. Precedence: a connected socket (Prompt Deck's `text`) replaces the box; else the box. With no words at all Cue still works from the picture alone: for video, what naturally happens next; for image targets, a faithful re-prompt of the picture. Quoted text is dialogue, kept verbatim. |
| `notes` | multiline STRING | standing notes, saved in the workflow: world, house rules, LoRA trigger words. Sent with every request. |
| `target` | combo | scanned from `templates/`. v1 ships `MiniMax H3`, `Krea 2`, `Generic`. |
| `mode` | combo | only when the template declares modes. H3: `t2v`, `i2v`, `first+last`, `last`, `ref2v`. |
| `duration_seconds` | FLOAT, also a socket | only when the template declares a timeline. H3 range 4–15 s. Cue owns it: a 15 s prompt is planned in more shots and beats than a 5 s one. Also an output, linked into the Director's duration (§10.2). |
| `dialogue_language` | STRING | default `English`. Goes into the `<d>[Language] …</d>` tag; the body stays English. |
| `detail` | combo `short / medium / long` | default comes from the template. H3 wants long, Krea 2 medium, tags short. |
| `writer` | combo | installed Ollama models + a recommended list. Picking an uninstalled one pulls it. |
| `endpoint` | STRING | default `http://127.0.0.1:11434`. |
| `seed` | INT with `control_after_generate` | the writer's seed; reroll = new seed. |
| `image` | IMAGE, optional | i2v start frame, first frame for first+last, or an optional picture for image targets. Image targets need no picture at all: "cute woman in a bikini" or "superman saving people" in `intent` is enough. |
| `image_last` | IMAGE, optional | last frame for `first+last` / `last`. |
| `ref_1 … ref_9` + `label_1 … label_9` | IMAGE + STRING | shown in `ref2v`. Same label on several slots = one subject with several pictures. Max 9 matches ComfyUI's native H3 reference node. |
| `subjects_override` | STRING, socket | optional lines `token = description`; replaces a subject's description at resolve time, no model call. Feed it from a second Prompt Deck (a characters file) for actions × characters. |
| `lock` | toggle | locked: the result box is editable and used verbatim, no model call. |

### 3.2 The result area

Same idea as Prompt Deck's `resolved` box and Keeper's bar:

- The generated prompt fills a box in the node, **streamed as the model writes it**, so a bad
  start is visible within seconds. ComfyUI's interrupt cancels the stream.
- Slim bar: `◀ 2/5 ▶` history of the last N prompts for this node (default 10, seed stored with
  each), `↻` reroll (new seed), `🔒` lock. Locking freezes the shown prompt; editing is allowed
  while locked.
- A warning badge when validation (§7) failed and the best attempt is shown anyway.
- The final text used is written back into the box after each run, so the real prompt lands in
  the workflow JSON / PNG metadata (honest metadata, as in Prompt Deck).

### 3.3 Outputs

| output | meaning |
| --- | --- |
| `prompt` | the finished prompt, subject tokens resolved to their descriptions |
| `prompt_tokens` | the same prompt with subject tokens left in place (for Prompt Deck search→replace or a later node) |
| `subjects` | one line per subject: `token = description` |
| ~~`image`, `image_last`, `ref_1 … ref_9` pass-through~~ | **as built:** dropped for v1. The Director takes media from its own timeline, so pass-through only served native graphs; wire the same Load Image to both nodes there. |
| `duration_seconds` | FLOAT, link it to the Director's `duration` widget so both agree without typing twice |
| `mode` | STRING (`T2VA`, `I2VA`, `FL2VA`, `L2VA`, `REF2VA`), to link into the Director's mode once that link is confirmed (§12) |

Dynamic sockets/widgets (mode box, ref slots, duration) are JS-side, as PromptDeck already does
for its list; the Python side accepts all of them as optional.

## 4. Subject tokens

Omer's requirement: the body must say "man", not "man with glasses and dark hair and a grey
coat", so a character can be swapped later without re-asking the model.

- Every subject has a **delimited token** and **one description**. Bare-word replacement is
  forbidden: "man" is inside "woman" and "many".
- H3 `ref2v`: the token is `<Subject N>` (native to the format), numbered in slot order; the
  description is the `subject_definitions` line. The body uses the token only.
- Everything else: the token is `{label}` (lowercased, spaces → `_`). The model writes the body
  with tokens; the node substitutes descriptions when producing `prompt` and leaves them in
  `prompt_tokens`.
- No reference pictures: the model is still asked to name the subjects it found in the intent
  (`{superman}` = "a muscular man in a blue suit with a red cape…"), so one replace turns
  Superman into Batman.
- Payoff beyond swapping: generate "fighting" once, run it against five characters. Model calls
  drop from actions × characters to actions + characters.
- Validation: every token used in the body is defined; every defined token is used (warning only).
- **As built, substitution policy:** the first mention of a token gets the full description, later
  mentions get a short handle ("the man", "Superman"), so a 20-word description is not pasted at
  every mention. Descriptions of person-like tokens are cut to appearance only (before
  "standing / holding / looking …", capped at 32 words) because small models let the scene leak
  into them. If a defined token is never used but its bare word is in the body, the first
  whole-word occurrence is tokenized (word boundaries, so "man" never matches "woman").

## 5. Reference and start-image handling

Identity comes from the target model's reference inputs, so Cue describes pictures only as much
as the format needs:

| picture | how much | why |
| --- | --- | --- |
| a person in a ref slot | one line: hair, build, clothes | H3 requires a definition line per subject and a description at first appearance; "undressing" must know what comes off |
| a scene / place in a ref slot | a few lines: layout, light, key props | the body has to stage action in it |
| i2v / first-frame image | full look: style, subjects, composition, scene anchors | the guide wants these established before the action starts |
| any picture for an image target (Krea 2, SDXL…) | full look | no reference input exists there; the description *is* the character |

Rules:
- **Label length decides.** A one-or-two-word label ("man") → the node describes the picture. A
  sentence ("man in a tuxedo, no glasses") → used as written; the picture stays for identity only.
- Descriptions are **cached per image** (hash of the downscaled bytes + writer + detail). Changing
  the action never re-reads the pictures. Server-side JSON next to `recent_files.json`, capped.
- Images are downscaled to a 1024 px long edge before base64. Qwen3-VL cost scales with pixels.

## 6. Writer backend (Ollama)

Verified against Ollama's API docs on 2026-10-24:

| need | endpoint |
| --- | --- |
| list installed models | `GET /api/tags` |
| pull a model with streamed progress | `POST /api/pull` (`status`, `completed`, `total`; cancelled pulls resume) |
| chat with images | `POST /api/chat`, `images` = base64 list on the message, streamed |
| free VRAM right after the answer | `keep_alive: 0` on the request |

- **Default writer:** `huihui_ai/qwen3-vl-abliterated:4b-instruct` (3.3 GB, Q4_K_M).
  Alternatives in the recommended list: `:2b-instruct` (1.9 GB) and the 9b build (tag to verify).
  Instruct variants only; any `<think>…</think>` block is stripped defensively.
- VRAM: the writer unloads the moment it answers, and ComfyUI loads the image/video model lazily
  afterwards, so they never share the card. Size matters for speed and reliability, not memory.
- Options: `temperature` 0.7, `seed` from the widget, `num_predict` per detail level, timeout
  120 s with the stream as heartbeat.
- Pull progress is shown in the node via the server's websocket messages, like Keeper's saves.
- **Failure text when unreachable:** "Ollama not reachable at `<endpoint>`. Install it from
  ollama.com (it is already on the Windows box), then click ↻." Plus a section in
  `WINDOWS_SETUP.md`.
- The model card calls the abliteration crude; expect an odd sentence now and then. Reroll.

## 7. Scaffolding, validation, retry

The node writes everything fixed itself and asks the model for creative text only. That is what
makes a 4B model realistic for H3.

### 7.1 MiniMax H3 (format from MiniMax's guides)

| mode | first line (written by the node) | sections (model writes, node checks) |
| --- | --- | --- |
| `t2v` | none | `integrated_multimodal_description`, `overall_soundscape`, `non_diegetic_music` |
| `i2v` | `For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.` | same three |
| `first+last` | `How the reference pictures align with the target video — Picture 1 (from Shot 1) aligns with the 0.00-second mark …; Picture 2 (from Shot N) aligns with the S.SS-second mark …` | same three |
| `last` | `How the reference pictures align with the target video — <Picture 1> (from [Shot N]) aligns with the S.SS-second mark …` | same three |
| `ref2v` | none; node emits `subject_definitions:` lines from labels + cached descriptions | `summary`, `retention_analysis`, `detailed_description`, `overall_soundscape`, `non_diegetic_music` |
| `continuation (DaSiWa)` | none | the next visible action only, plain prose: the Director's own continuity wraps it with its seam policy (§10.2) |
| `continuation (Reel)` | none | the whole window prompt: seam policy (same uninterrupted shot, keep identity, clothing, lighting, no cut or restart) + the new visible action, timed to `duration_seconds` (§10.3) |

Shared rules the template carries: shots as `[Shot N]`, cuts as `At 00:04.500`, camera as
motion + amplitude + speed, speakers get stable `(S1)` IDs, dialogue as
`<d>[Language] …</d>` verbatim, voiceover uses the exact phrase `says in an off-screen voiceover`
followed by "lips remain closed", body in English, no abstract words ("cinematic", "beautiful").
S.SS is the duration with two decimals; `[Shot N]` in the last-frame line is the real final shot.

### 7.2 Krea 2

Open weights since June 2026 (Raw and Turbo), Qwen3-VL text encoder: long full-sentence prompts,
most important content first, no `(word:1.3)` weighting (degrades above ~1.2). Comfy's Krea 2
workflow templates ship a "Prompt Enhancer" system instruction from the Krea team; the template
starts from it (check its licence before copying text). Style-LoRA trigger words belong in
`notes`.

### 7.3 Generic

Plain descriptive prose, subject first, one paragraph per detail step. The fallback for whatever
model wins next; a proper template is a text file away.

### 7.4 Validation (H3), then one retry

1. Required sections present, in order, exactly named.
2. Every `<Subject N>` / `<Picture N>` / `{token}` in the body is defined; nothing undefined.
3. Timeline: no timestamp beyond `duration_seconds`; description length matches the detail level.
4. Dialogue: every quoted line from the intent appears verbatim inside a `<d>` tag with the chosen
   language.
5. Fail → one retry with the error list appended to the request. Fail again → show the best
   attempt with the warning badge; never block the queue.

**As built, repairs before validation** (small models need them): a user-quoted line left in
plain quotes is wrapped into `<d>[Language] …</d>`; looping output is cut where a clause pair
repeats; field names are also accepted inline; a timestamp at the clip end counts as an error
only when it is a cut (`[Shot N] At …` or "cuts to"), a beat there is a warning; if no field was
found at all, the text becomes the description so the box is never empty. Token budget is
derived from the detail level's word count, `repeat_penalty` 1.15, `num_ctx` 16384, `keep_alive` 0.

### 7.5 As built in 1.7: JSON protocol, beat budget, repairs

- **The writer answers JSON against a schema** (Ollama `format`), so the structure can no longer
  break: keys in generation order are `subjects` (token + description), `beats`
  (start_seconds + action, timeline targets only), then the prose fields (`integrated_multimodal_description`,
  `overall_soundscape`, `non_diegetic_music`; the five ref2v fields; `prompt`; or `next_action`).
  Strings have `minLength` 1 so an empty sound field cannot pass. A truncated answer is
  salvaged key by key; a writer that ignores the schema falls back to the old text parser.
- **Beat budget:** one action per 2.5 s (5 s → 2, 10 s → 4, 15 s → 6) and a shot budget (≤5.5 s one
  shot, ≤10.5 s two, else three), both stated in the request. The model plans `beats` before
  it writes, the validator rejects more beats than the budget, beats past the end, and more
  shots than allowed; the retry message names the budget.
- **Repairs after a failed retry** (format only, never content): a cut at the clip end is
  dropped, shots beyond the budget lose their marker and run on as one shot, a description
  without `[Shot 1]` gets it, single- and double-quoted lines become `<d>` speech.
- **Examples removed from the system prompt.** Small writers copied them verbatim (the kitchen
  mug scene turned up in a bar fight). The schema carries the format now.
- The node streams a readable preview from the partial JSON (the prose field being written),
  not raw JSON.

## 8. Templates folder

`templates/<name>.md`, scanned at startup and on ↻. A header block of `key: value` lines, then
sections:

```
kind: video            # or image
modes: t2v, i2v, first+last, last, ref2v
timeline: yes          # shows duration_seconds
detail: long           # default dial position
## system              # who the writer is, output rules
## scaffold t2v        # optional fixed text per mode, with {intent} {notes} {duration} {language} {subjects} {detail}
## rules               # bullet rules appended to the system text
## examples            # one or two short examples (few-shot)
```

Adding WAN, LTX or a new image model = a new file. The `Generic` file is the documented
starting point.

## 9. Caching and execution

- Prompt cache key: hash of (target, mode, intent, notes, language, duration, detail, writer,
  seed, image hashes, labels). Same key → no call. Reroll changes the seed.
- `IS_CHANGED` follows that key plus the lock state. Locked → the box text is the output and no
  request is made.
- History lives in the workflow (widget value, capped), descriptions server-side.

## 10. Where Cue sits, and the DaSiWa Director

### 10.1 Prompt Deck → Cue → conditioning

Cue does not replace Prompt Deck, and nothing forces it between Prompt Deck and the rest.

| node | job |
| --- | --- |
| Prompt Deck | which line runs: the visible list, transport, hold, ratings, text surgery, filename slug |
| Cue | turning a short line (plus pictures) into the target model's format |
| Director / native H3 nodes / CLIP encode | building the conditioning |

- Prompt Deck reads a file and has no text input, so the chain is Deck `text` → Cue `intent`.
  Deck → Director with hand-written full prompts keeps working; Cue is optional in between.
- Actions × characters: one Deck feeds an actions file into `intent`, a second Deck feeds a
  characters file into `subjects_override`. Cue resolves tokens without a model call, so the
  writer runs once per action, not once per combination. Deck's `filename_slug` still names the
  output files.
- Cue's cache means stepping through Deck lines costs one writer call per new line and nothing
  on repeats; the Director's Forge is a modal with an Apply button and cannot be driven this way.

### 10.2 Feeding the Director (verified in DaSiWa source, 2026-10-24)

The chain is fixed: **Prompt Deck → Cue → Director**. Cue's `prompt` goes into the Director's
optional `external_prompt_overwrite` socket (STRING, `forceInput`). For an ordinary take that
socket wins over the Director's prompt field and builder state; the only rewrite is RefMod alias
translation, and Cue emits no aliases.

- **Duration is Cue's.** Set it on Cue and link Cue's `duration_seconds` output to the
  Director's `duration` widget (current frontends take a link on any widget). One number, and
  Cue knows it, which it must: a 15 s prompt is planned in more shots and beats than a 5 s one.
- **Mode:** set on both until the `mode` link into the Director's combo is confirmed (§12).
- **Picture numbering is the Director's.** Images, videos and audio are numbered within type in
  timeline order, and the Director takes media from its own timeline only. Cue's ref slot N must
  be the Director's N-th image tile; the node tooltip says so.
- If a RefMod row has a description the Director appends "Reference descriptions:" lines after
  the prompt. Timeline tile descriptions are not appended. The Director's `positive_prompt`
  output shows the final text.
- **DaSiWa continuity ignores the socket.** It composes its own prompt from the continuity
  settings (window-context sentence + seam policy + `Next action:`), and the Guide recomposes
  it again. For those chains use the `continuation (DaSiWa)` row and paste the text into the
  Director's continuity prompt field. Reel chains do not have this problem (§10.3).

Considered and dropped: Cue between Director and Guide, editing the guide dict. It would have
read mode, duration and pictures from the Director, but Cue goes before the Director, and it
rode on DaSiWa's internal dict.

### 10.3 Native nodes and the Reel

- No Director: Cue's prompt goes into `MiniMaxH3ImageToVideo` / `MiniMaxH3ReferenceToVideo`,
  its ref pass-through outputs wire into the reference node's sockets in order, and Cue's own
  mode and duration apply. Set the same frame count on the empty-latent node; Cue's header shows
  the frames it wrote for.
- Reel (`docs/H3_REEL_PLAN.md`: latent-level extend, prequel, bridge, loop, cut, polish;
  waiting for its go-word): the Director runs in a normal mode with continuity off, so the
  socket is honoured and Cue writes the whole window prompt with the `continuation (Reel)` row,
  seam policy included. The seam snippet the Reel plan put in Prompt Deck's pre-text moves into
  Cue's H3 template. Chain: Deck → Cue → Director → Guide → Reel Guide → sampler → Reel Splice.

## 11. Where it is tested

Coding happens on the MacBook Air. The RTX 5090 ComfyUI box is Windows, already has Ollama, and
pulls the repo via git (driven by a separate Claude session there). The setup steps, the
test gate and the overnight batches with a judge model are in `docs/WINDOWS_RUNBOOK.md`.

| on the Air | on the Windows box |
| --- | --- |
| node imports in the sandbox ComfyUI | real H3 i2v / ref2v runs with the generated prompts |
| templates + validator against the 2B model, if Ollama is installed locally | 4B vs 2B format reliability |
| JS widgets, history, lock, streaming | Turkish dialogue (H3 lists 11 stable languages; Turkish is "varying degrees") |
| | Director through the socket with the duration link, and Reel chains with `continuation (Reel)` |

Acceptance for v1: 10 varied intents through H3 `i2v` with the 4B writer, at least 8 valid on the
first attempt, 10 after the retry.

## 12. Open questions

- Name: Cue is the working name.
- Does the abliterated 9B Qwen3-VL tag exist on Ollama? (2B and 4B verified.)
- Krea 2 Prompt Enhancer text: lift from Comfy's template or write our own (licence check).
- Does a STRING link into the Director's `mode` combo work on the installed frontend? If not,
  mode is set on both nodes.
- Judge model for the overnight batches: the largest abliterated Qwen3-VL that fits on 32 GB
  with ComfyUI unloaded (30B-A3B or 32B at Q4); the runbook records the tag that exists.
- ~~`ref2v` with a start frame~~ **as built:** a ref slot labelled `first frame` (or `start`,
  `last frame`, `end`) is defined as a picture anchor, `<Picture N> is the first frame of [Shot 1],
  showing …`, not as a subject; the `image` socket in ref2v does the same and becomes picture
  refs+1 (the bar warns which Director tile it must be).
- Expose `temperature`? Default hidden.

## 13. Sources

- MiniMax H3 model card and guides: `huggingface.co/MiniMaxAI/MiniMax-H3`,
  `docs/VIDEO_PROMPT_WRITING_GUIDE_base_en.md`, `docs/VIDEO_PROMPT_WRITING_GUIDE_ref_en.md`
- MiniMax agent skill: `github.com/MiniMax-AI/MiniMax-H3/tree/main/skills/h3-prompt-writing`
- ComfyUI native nodes: `comfy_extras/nodes_minimax_h3.py` (`MiniMaxH3ReferenceToVideo` uses
  `<Picture i>` tags in socket order, max 9 images)
- DaSiWa Director (GPL-3, read not copied): `nodes/nodes_minimax_h3_director.py`,
  `nodes/nodes_minimax_h3_director_guide.py`, `nodes/h3_continuity/core.py`,
  `docs/minimax_h3_director.md`, `docs/h3_continuity.md`
- Ollama API: `github.com/ollama/ollama/blob/main/docs/api.md`
- Writer models: `ollama.com/huihui_ai/qwen3-vl-abliterated` (2b, 4b-instruct)
- Krea 2: `blog.comfy.org/p/krea-2-open-source-models-are-now`,
  `instasd.com/post/krea-2-prompt-and-style-guide-comfyui`,
  `comfyui-wiki.com/en/tutorial/advanced/image/krea/krea-2`
