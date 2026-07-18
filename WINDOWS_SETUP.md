# Windows setup (for Claude, or humans in a hurry)

Target machine: the Windows box running the real ComfyUI
(`C:\Users\omerv\Documents\ComfyUI`).

## Steps

1. Clone into the custom nodes folder:

   ```powershell
   cd C:\Users\omerv\Documents\ComfyUI\custom_nodes
   git clone https://github.com/omervaner/ComfyUI-PromptDeck
   ```

2. Restart ComfyUI (fully — the server process, not just a browser refresh).

3. That's it. **No pip installs, no requirements.txt** — the node is pure
   Python stdlib plus one frontend JS file that ComfyUI serves automatically
   via `WEB_DIRECTORY`.

## Verify

- Add the node: right-click canvas → Add Node → **PromptDeck → Prompt Deck 🎴**
  (or double-click and search "prompt deck").
- Point `file_path` at an existing prompt file, e.g.
  `C:\Users\omerv\Documents\ComfyUI\workspace\prompts_newer.txt`
- The list should populate immediately (no queue needed). If it says
  "file not found", the path is wrong; backslashes are fine as-is.
- Hit **Queue** with nothing connected — the node is an output node and runs
  standalone: the highlight should land on the current line and the `resolved`
  box should fill with the composed prompt.

## Update later

```powershell
cd C:\Users\omerv\Documents\ComfyUI\custom_nodes\ComfyUI-PromptDeck
git pull
```

then restart ComfyUI.

## Migrating from WWAA Advanced Text File Reader

Same file works unchanged. Mapping:

| WWAA | Prompt Deck |
|---|---|
| file path | `file_path` (same txt file, same folder) |
| traversal_mode forward/reverse/random | `line_index` control: increment / decrement / randomize |
| hold_current_text | **hold** button (or control: fixed) |
| starting_index | type it into `line_index`, or click the line in the list |
| reset_counter | set `line_index` to 0 / click line 1 |
| reload_file | ↻ button (file changes are also picked up automatically) |
| Search and Replace node | `replacements` box (`search -> replace`, one per line) |
| JoinString pre/after | `pre_text` / `after_text` boxes |
| skip_lines | not carried over (was unused) |
