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

## Install

```
cd ComfyUI/custom_nodes
git clone https://github.com/omervaner/ComfyUI-PromptDeck
```

Restart ComfyUI. The node is under **PromptDeck → Prompt Deck**.

No pip installs. See [WINDOWS_SETUP.md](WINDOWS_SETUP.md) for a hand-holding version.

## Notes

- `line_index` counts **runnable** lines (enabled + passing the rating filter),
  wraps around forever, and any value is safe (it's taken modulo the count) —
  so `randomize` gives you random lines for free.
- The header shows `position / total`; watch it to see the file running out.
- Editing the txt while ComfyUI runs is fine — the node checks the file's
  mtime and picks up changes on the next run; ↻ refreshes the list view.
