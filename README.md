# Prompt Deck

A ComfyUI custom node that feeds prompts line-by-line from a text file — and
actually **shows you the list**. Current line highlighted, click to jump, hold,
per-line disable, ratings, and text surgery (pre/after text, search→replace,
head/tail cutting). Built as a full replacement for file-reader prompt feeders.

![node](https://img.shields.io/badge/ComfyUI-custom%20node-blue) ![deps](https://img.shields.io/badge/dependencies-none-brightgreen)

## Why

Every prompt feeder out there is blind: you're on line 531 of a file and you
have no idea what line 531 says until the image comes out wrong. Prompt Deck
renders the file inside the node, marks what's running and what's next, and
gives you transport controls.

## Features

- **Visible prompt list** in the node: numbered rows, the currently running
  line highlighted, the next-up line marked. **Resize the node and the list
  grows** (the text boxes stay put). Hovering a truncated row shows the
  **full prompt in a floating box**. The header shows
  `position / total · word count`.
- **Scrolls like it should**: mouse wheel over the list scrolls it (with
  acceleration — flick gently or spin through thousands), the scrollbar thumb
  drags, and clicking the track pages. Scrolling pauses auto-follow; jumping,
  stepping, or queueing re-engages it.
- **Click a line to jump** to it. ◀ ▶ step backward/forward. **hold** pins the
  current line (repeat until released). ↻ reloads the file.
- **Ratings**: ★ good / ✕ bad per line, saved to a sidecar file
  (`yourfile.txt.deck.json`) next to your prompt file — they survive restarts,
  workflow switches, and file reordering (keyed by line text, not line number).
- **Rating filter**: run `all`, `good only`, or `hide bad`.
- **Per-line disable** checkboxes (stored in the workflow).
- **Recent files**: the `recent ▾` button under the path lists the last 10
  prompt files you loaded (remembered server-side, survives restarts) — one
  click to switch between your scattered prompt collections.
- **Restart-proof position**: the line index is a widget, saved *in your
  workflow*, not hidden server state. Come back a week later, it's where you
  left it. Batch-queue 20 runs → 20 different lines (native
  `control_after_generate` machinery: increment/decrement/randomize/fixed).
- **Text surgery**, applied in this order to each line:
  1. leading list numbering stripped (`12.`, `12)`, `12:`, `12 -`)
  2. `cut_to` — comma-separated markers; cuts the line's **head** up to and
     including the earliest match
  3. `cut_from` — comma-separated markers; cuts the line's **tail** from the
     earliest match onward (e.g. `cinematic, film grain` to drop style tails)
  4. `search` / `replace` — plain text substitution
  5. `pre_text` + line + `after_text`
- **Honest metadata**: the final composed prompt is written back into the
  read-only `resolved` box after each run — so the *actual* prompt used is
  saved in your workflow JSON / PNG metadata, instead of the stale greyed-out
  text in a connected prompt box.
- **No line limits.** Blank lines and `# comment` lines are skipped.
- **Runs standalone** (it's an output node): a workflow with just this node
  executes — handy for cycling/testing prompts without loading any models.
- The node is under **PromptDeck → Prompt Deck** (display name has no emoji;
  emoji are a chat-only indulgence 🎴).
- **Zero dependencies.** Python stdlib + one JS file.

## Outputs

| output | meaning |
|---|---|
| `text` | the final composed prompt |
| `filename_slug` | filesystem-safe slug of the line — wire into a Save node's filename so images are named after their prompt |

(Position, totals, and remaining lines are shown in the node header instead of
cluttering the outputs.)

## Keeper 💾 (second node)

Save Image keeps *everything*; Preview Image makes you right-click → open →
save the good ones by hand. **Keeper** previews like Preview Image and saves
only what you tell it to.

- The image fills the node, like Save Image. One slim bar underneath:
  `◀ 3/10 ▶` on the left, `all`, `Save as…` and `💾` on the right.
- **💾** saves the shown image to the output folder using `filename_prefix` —
  same formatting as Save Image (`MyModel/%date:yyyy-MM-dd%/%date:hhmmss%`),
  dates resolved at queue time, so the name reflects when it was generated.
  No dialogs. A ✓ appears on saved images; 💾 won't save the same one twice.
  **Double-click the image** or press **S** (node selected) to do the same —
  handy when you're zoomed in and the bar is off-screen.
- **Save as…** asks for a name (pre-filled with the auto one). Slashes make
  subfolders; it never overwrites (appends `_2`, `_3`…).
- **all** toggles save-everything mode: every image is written to output at
  generation time, exactly like Save Image (arrives with the ✓ already on).
  Stored in the workflow.
- **History**: the last 5 / 10 / 20 / 50 images — click the `3/10` counter to
  pick. ◀ is older, ▶ newer; ←/→ keys work while the node is selected. A new
  run jumps to the first image of the new batch. Only the current image and
  its neighbours stay decoded, so 50 won't eat your laptop's RAM.
- Saved PNGs keep the workflow/prompt metadata, so they drag back into
  ComfyUI like any Save Image output.
- Unsaved previews live in ComfyUI's temp folder and vanish on restart —
  that's the point. History is per page session (a browser refresh clears it).
- Passes `images` through, so it can sit mid-chain.

Under **PromptDeck → Keeper**.

## Cue 🎬 (third node)

Type an intent — `undressing`, `fighting`, `superman saving people`, or a whole brief — plug in
a picture or not, pick the target model, and a small **local** vision model writes the prompt in
the format that model actually wants. No hosted API: it talks to [Ollama](https://ollama.com) on
your machine with an abliterated Qwen3-VL (2B/4B), so violence and undress-and-cut-to-black
scenes are not refused. The model is unloaded the moment it answers, so it never shares the GPU
with your image or video model.

- **MiniMax H3** gets the exact rewriter format local H3-Base was trained on (MiniMax never
  open-sourced the rewriter): `t2v`, `i2v`, `first+last`, `last`, and `ref2v` with labelled
  reference slots (`ref_1…9` + `label_1…9`; three slots labelled `woman` become one
  `<Subject 1>` defined from three `<Picture N>`s). The node writes the fixed scaffolding itself
  (header lines, `subject_definitions`), the model writes the creative text, and the node
  validates sections, labels, timestamps and dialogue, retrying once. Label a ref slot
  `first frame` (or plug the start picture into `image`) and ref2v starts from that frame. Two continuation flavours:
  `continuation (DaSiWa)` writes only the next action for the Director's continuity field,
  `continuation (Reel)` writes a whole window prompt with the seam policy.
- **Krea 2** and **Generic** get prose. A new target model is a new text file in `templates/`.
- **Subjects are swappable tokens.** The body says `{man}`; the description lives in one place.
  `prompt` has the descriptions filled in, `prompt_tokens` keeps the tokens, and
  `subjects_override` (lines like `man = tall woman with red hair`) swaps a character without a
  model call. Two Prompt Decks, one of actions into `intent` and one of characters into
  `subjects_override`, give actions × characters with the writer running once per action.
- **Dialogue**: quoted text in the intent lands verbatim in `<d>[Language] …</d>`;
  `dialogue_language` sets the tag. **Duration** shapes the shot plan (a 15 s prompt is not a
  5 s prompt); the `duration_seconds` output can be linked into the Director's duration.
- **Standing notes** travel with every request and live in the workflow: world, house rules,
  LoRA trigger words. Nothing project-specific is in the code.
- The prompt **streams into the node** as it is written. Slim bar: `◀ 2/5 ▶` history, `↻`
  reroll (new seed and queue), `🔒` lock (the box becomes editable and is used verbatim), `⚠`
  lists validation warnings. `models ▾` lists Ollama's installed models and pulls a recommended
  one with progress.
- Pictures are described once and cached by content; prompts are cached by all inputs, so
  re-queues and Prompt Deck repeats cost nothing.
- Runs standalone (output node), so you can iterate prompts without loading any model.
- Feeding the DaSiWa Director: `prompt` → the Director's `external_prompt_overwrite` socket.
  Reference slot N must be the Director's N-th image tile. Its continuity ignores that socket;
  paste the `continuation (DaSiWa)` text into its continuity prompt field.

Under **PromptDeck → Cue**. Needs Ollama running (`ollama serve`); the node tells you if it is not.
Design notes in [docs/CUE_DESIGN.md](docs/CUE_DESIGN.md).

## Install

```
cd ComfyUI/custom_nodes
git clone https://github.com/omervaner/ComfyUI-PromptDeck
```

Restart ComfyUI. The nodes are under **PromptDeck → Prompt Deck**, **PromptDeck → Keeper** and **PromptDeck → Cue**.

No pip installs. Cue additionally needs [Ollama](https://ollama.com) running locally. See [WINDOWS_SETUP.md](WINDOWS_SETUP.md) for a hand-holding version.

## Notes

- `line_index` counts **runnable** lines (enabled + passing the rating filter),
  wraps around forever, and any value is safe (it's taken modulo the count) —
  so `randomize` gives you random lines for free.
- The header shows `position / total`; watch it to see the file running out.
- Editing the txt while ComfyUI runs is fine — the node checks the file's
  mtime and picks up changes on the next run; ↻ refreshes the list view.
