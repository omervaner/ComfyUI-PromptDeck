# tests/ — overnight driver for Cue and Reel

Protocol: `docs/WINDOWS_RUNBOOK.md`. Gate: nothing is queued on the Windows box until Omer says
**LANTERNS MIGHT**. The script does not enforce that; `--dry-run` is always the first command.

## Run

```powershell
cd C:\Users\omerv\Documents\ComfyUI\custom_nodes\ComfyUI-PromptDeck
python tests\overnight.py --selftest                  # offline, no servers
python tests\overnight.py --dry-run --all             # /object_info check of every batch, queues nothing
python tests\overnight.py cue-format                  # one batch: queue, /free, judge, report
python tests\overnight.py cue-format --all            # that batch and every one after it, runbook order
python tests\overnight.py reel-anchors --judge-only   # re-judge / rebuild reports without queueing
```

Options: `--comfy http://127.0.0.1:8188`, `--ollama http://127.0.0.1:11434`,
`--out <dir>` (default `..\..\output\promptdeck_tests`, i.e. ComfyUI's output folder),
`--judge-model <tag>` (overrides the batch), `--timeout`, `--judge-timeout`, `--rejudge`.
Re-running a batch skips items whose `run.json` has `status: ok`. Errors, interrupts and timeouts
are recorded per item and the batch continues; three unreachable queues in a row stop the batch.

## Batch file (`tests/batches/<name>.json`)

- `workflow`: path to an API-format graph, or `{"by": "<var>", "map": {value: path}}` to pick a
  graph per item (e.g. one workflow per `mode` or `op`). Paths are relative to the repo.
- `matrix`: list-valued keys form the cartesian product (a key `modes` is readable as `{modes}` or
  `{mode}`); scalars pass through unchanged. `intent_file` (one intent
  per line, blank lines and `#` comments skipped, leading `12.` numbering stripped) or `intents`
  becomes `{intent}` plus `{intent_index}`. A list entry may be a dict, reached as `{source.file}`.
- `set`: `[{"node": "<_meta.title or id>", "input": "<name>", "value": <literal or "{var}">}]`.
  Title match first, then id. A `{var}` alone keeps its type; inside text it is formatted in. A
  missing node, input or variable fails loudly. Optional `"when": {"mode": "ref2v"}` limits an
  entry to matching items; `"create": true` allows an input the export does not carry.
- `prune_to`: `["Cue"]` keeps only those output nodes and what feeds them (prompt-only batches).
- `outputs.node`: node whose ui text is taken as the prompt (else the first text output found).
- `judge`: `kind` video|image|prompt, `seam_frames`, `primary` (sort key), `seams` (list of
  seconds, or `{"by": "op", "extend": [5.0], ...}`), `refs` (folder or file of reference pictures,
  `{var}` allowed). `judge_model`: Ollama tag; `timeout_seconds` per item. `comment` keys are ignored.

Each item gets a stable id: a short slug of its values plus a hash, so folders survive re-runs.

## Outputs (`<out>/<batch>/`)

- `<item>/run.json` — matrix values, status (ok | error | interrupted | timeout), prompt text,
  validator fields if the node's ui output carries any, files, timings, errors; plus
  `history.json`, `prompt.txt` and the copied outputs (mp4 / png).
- `<item>/judge.json` — scores 1–5 (`intent_match`, `prompt_match`, `identity_match`, `seam`,
  `artifacts`), `note`, the raw model answer, which frames were sent (`frames/` via ffmpeg when it
  is on PATH, otherwise the stills the workflow saved) and which reference pictures.
- `results.csv` — one row per item: id, matrix values, status, seconds, `v_*` validator columns,
  scores, note, paths. `index.html` — contact sheet sorted by the batch's primary score. `log.txt`.

The judge prompt is `tests/judge_prompt.md`; keep it unchanged across batches so runs compare.
