import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const BAR_H = 22;
const PAD = 4;
const BTN_W = 22;
const HISTORY_MAX = 10;
const MAX_REFS = 9;

const COLORS = {
  btn: "#2a2a33",
  btnHover: "#3a3a46",
  btnActive: "#7a3b3b",
  text: "#c8c8d0",
  textDim: "#55555e",
  warn: "#e8c256",
  error: "#c05555",
  locked: "#7a3b3b",
};

function widget(node, name) {
  return node.widgets?.find((w) => w.name === name);
}

// ---- show / hide stock widgets (Keeper pattern) ----
function setVisible(w, visible) {
  if (!w) return;
  if (visible) {
    if (w._cueHidden) {
      w.hidden = false;
      if (w._cueComputeSize) w.computeSize = w._cueComputeSize;
      else delete w.computeSize;
      if (w.inputEl) w.inputEl.style.display = "";
      w._cueHidden = false;
    }
  } else if (!w._cueHidden) {
    w._cueHidden = true;
    w._cueComputeSize = Object.prototype.hasOwnProperty.call(w, "computeSize") ? w.computeSize : null;
    w.computeSize = () => [0, -4];
    w.hidden = true;
    if (w.inputEl) w.inputEl.style.display = "none";
  }
}

function templateInfo(node) {
  const name = widget(node, "target")?.value;
  return node._cue.templates[name] || null;
}

function refLinked(node, n) {
  const input = node.inputs?.find((i) => i.name === `ref_${n}`);
  return !!(input && input.link != null);
}

function refresh(node) {
  const c = node._cue;
  if (!c) return;
  const tpl = templateInfo(node);
  const hasModes = !!(tpl && tpl.modes && tpl.modes.length);
  const timeline = !!(tpl && tpl.timeline);
  setVisible(widget(node, "mode"), hasModes);
  setVisible(widget(node, "duration_seconds"), timeline);
  setVisible(widget(node, "dialogue_language"), !tpl || tpl.kind === "video");
  for (let n = 1; n <= MAX_REFS; n++) setVisible(widget(node, `label_${n}`), refLinked(node, n));
  setVisible(widget(node, "history"), false);
  setVisible(widget(node, "lock"), false);
  const res = widget(node, "result");
  if (res?.inputEl) {
    const locked = !!widget(node, "lock")?.value;
    res.inputEl.readOnly = !locked;
    res.inputEl.style.borderColor = locked ? COLORS.locked : "";
    res.inputEl.placeholder = locked ? "locked: this text is the output" : "the prompt lands here";
  }
  node.setSize([node.size[0], Math.max(node.size[1], node.computeSize()[1])]);
  node.setDirtyCanvas(true, true);
}

// ---- history (stored in the hidden `history` widget as JSON) ----
function readHistory(node) {
  try {
    const v = JSON.parse(widget(node, "history")?.value || "[]");
    return Array.isArray(v) ? v : [];
  } catch {
    return [];
  }
}

function writeHistory(node, items) {
  const w = widget(node, "history");
  if (w) w.value = JSON.stringify(items.slice(-HISTORY_MAX));
}

function showEntry(node, idx) {
  const c = node._cue;
  const items = readHistory(node);
  if (!items.length) return;
  c.idx = Math.max(0, Math.min(items.length - 1, idx));
  const e = items[c.idx];
  const res = widget(node, "result");
  if (res) res.value = e.prompt || "";
  c.info = e.info || "";
  c.warnings = e.warnings || [];
  c.stage = "";
  node.setDirtyCanvas(true, false);
}

function onDone(node, d) {
  const c = node._cue;
  const items = readHistory(node);
  const entry = { prompt: d.prompt, tokens: d.tokens, subjects: d.subjects, seed: d.seed, info: d.info, warnings: d.warnings || [], ts: Date.now() };
  const last = items[items.length - 1];
  if (!(last && last.prompt === entry.prompt && last.seed === entry.seed)) items.push(entry);
  writeHistory(node, items);
  c.idx = Math.min(items.length, HISTORY_MAX) - 1;
  const res = widget(node, "result");
  if (res && !widget(node, "lock")?.value) res.value = d.prompt || "";
  c.info = d.info || "";
  c.warnings = d.warnings || [];
  c.stage = "";
  c.streaming = false;
  node.setDirtyCanvas(true, false);
}

// ---- writer models menu ----
async function pickModel(node, event) {
  const endpoint = widget(node, "endpoint")?.value || "";
  let data = { ok: false, installed: [], recommended: [] };
  try {
    const resp = await api.fetchApi(`/promptdeck/cue/models?endpoint=${encodeURIComponent(endpoint)}`);
    data = await resp.json();
  } catch {
    /* menu shows the error line below */
  }
  const writer = widget(node, "writer");
  const current = writer?.value;
  const items = [];
  if (!data.ok) items.push({ content: data.error ? `⚠ ${data.error.slice(0, 90)}` : "⚠ Ollama not reachable", disabled: true });
  for (const name of data.installed || []) {
    items.push({
      content: `${name === current ? "● " : "   "}${name}`,
      callback: () => {
        writer.value = name;
        node.setDirtyCanvas(true, true);
      },
    });
  }
  const missing = (data.recommended || []).filter((r) => !(data.installed || []).includes(r.name));
  if (missing.length) {
    if (items.length) items.push(null);
    for (const r of missing) {
      items.push({
        content: `↓ pull ${r.name}  (${r.note})`,
        disabled: !data.ok,
        callback: () => pullModel(node, r.name),
      });
    }
  }
  new LiteGraph.ContextMenu(items, { event, title: "writer model", className: "dark" });
}

async function pullModel(node, model) {
  const c = node._cue;
  c.pull = { model, status: "starting", completed: 0, total: 0 };
  node.setDirtyCanvas(true, false);
  try {
    await api.fetchApi("/promptdeck/cue/pull", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model, endpoint: widget(node, "endpoint")?.value || "", node: String(node.id) }),
    });
  } catch {
    c.pull = null;
    c.warnings = ["pull request failed"];
    node.setDirtyCanvas(true, false);
  }
}

function fmtGB(n) {
  return (n / 1e9).toFixed(1);
}

// ---- the slim bar under the result box ----
function drawButton(ctx, x, y, w, h, label, enabled, hover, active) {
  ctx.fillStyle = active ? COLORS.btnActive : enabled && hover ? COLORS.btnHover : COLORS.btn;
  ctx.beginPath();
  ctx.roundRect(x, y, w, h, 4);
  ctx.fill();
  ctx.fillStyle = enabled ? COLORS.text : COLORS.textDim;
  ctx.textAlign = "center";
  ctx.fillText(label, x + w / 2, y + h / 2 + 0.5);
}

function makeBarWidget(node) {
  return {
    name: "cue_bar",
    type: "PROMPTDECK_CUE_BAR",
    value: null,
    serializeValue: () => null,
    hits: [],
    computeSize(width) {
      return [width ?? 300, BAR_H + PAD * 2];
    },
    draw(ctx, drawNode, width, y) {
      const c = node._cue;
      if (!c) return;
      const barY = y + PAD;
      this.hits = [];
      const hp = c.hoverPos;
      const over = (x, w) => hp && hp[0] >= x && hp[0] <= x + w && hp[1] >= barY && hp[1] <= barY + BAR_H;
      ctx.save();
      ctx.font = "12px sans-serif";
      ctx.textBaseline = "middle";

      const items = readHistory(node);
      const n = items.length;
      let x = PAD;
      drawButton(ctx, x, barY, BTN_W, BAR_H, "◀", c.idx > 0, over(x, BTN_W));
      this.hits.push({ id: "prev", x, w: BTN_W });
      x += BTN_W;
      const counter = n ? `${c.idx + 1}/${n}` : "0/0";
      const counterW = ctx.measureText("10/10").width + 12;
      ctx.fillStyle = n ? COLORS.text : COLORS.textDim;
      ctx.textAlign = "center";
      ctx.fillText(counter, x + counterW / 2, barY + BAR_H / 2 + 0.5);
      x += counterW;
      drawButton(ctx, x, barY, BTN_W, BAR_H, "▶", c.idx < n - 1, over(x, BTN_W));
      this.hits.push({ id: "next", x, w: BTN_W });
      x += BTN_W + 4;
      drawButton(ctx, x, barY, BTN_W, BAR_H, "↻", !c.streaming, over(x, BTN_W));
      this.hits.push({ id: "reroll", x, w: BTN_W });
      x += BTN_W + 4;

      const locked = !!widget(node, "lock")?.value;
      let rx = width - PAD - BTN_W - 2;
      drawButton(ctx, rx, barY, BTN_W + 2, BAR_H, "🔒", true, over(rx, BTN_W + 2), locked);
      this.hits.push({ id: "lock", x: rx, w: BTN_W + 2 });
      if (c.warnings?.length) {
        rx -= BTN_W + 4;
        ctx.fillStyle = COLORS.warn;
        ctx.textAlign = "center";
        ctx.fillText(`⚠${c.warnings.length}`, rx + BTN_W / 2, barY + BAR_H / 2 + 0.5);
        this.hits.push({ id: "warn", x: rx, w: BTN_W });
      }

      // middle text: pull progress > stage > info
      let msg = "";
      let color = COLORS.textDim;
      if (c.pull) {
        const p = c.pull;
        msg = p.error ? `pull failed: ${p.error}` : p.done ? `pulled ${p.model}` : `pulling ${p.model.split("/").pop()} ${p.total ? `${fmtGB(p.completed)}/${fmtGB(p.total)} GB` : p.status}`;
        color = p.error ? COLORS.error : COLORS.text;
      } else if (c.stage) {
        msg = c.stage + "…";
        color = COLORS.text;
      } else if (c.info) {
        msg = c.info;
      }
      if (msg) {
        ctx.fillStyle = color;
        ctx.textAlign = "left";
        const maxW = rx - x - 10;
        let t = msg;
        while (t.length > 3 && ctx.measureText(t).width > maxW) t = t.slice(0, -2);
        if (t !== msg) t = t.slice(0, -1) + "…";
        if (maxW > 20) ctx.fillText(t, x + 4, barY + BAR_H / 2 + 0.5);
      }
      for (const h of this.hits) {
        h.y = barY;
        h.h = BAR_H;
      }
      ctx.restore();
    },
    mouse(event, pos) {
      if (event.type !== "pointerdown" && event.type !== "mousedown") return false;
      const [px, py] = pos;
      const hit = this.hits.find((h) => px >= h.x && px <= h.x + h.w && py >= h.y && py <= h.y + h.h);
      if (!hit) return false;
      const c = node._cue;
      switch (hit.id) {
        case "prev":
          showEntry(node, c.idx - 1);
          break;
        case "next":
          showEntry(node, c.idx + 1);
          break;
        case "reroll": {
          const seed = widget(node, "seed");
          if (seed) seed.value = Math.floor(Math.random() * 2147483647);
          app.queuePrompt(0, 1);
          break;
        }
        case "lock": {
          const w = widget(node, "lock");
          if (w) w.value = !w.value;
          refresh(node);
          break;
        }
        case "warn": {
          const items = (c.warnings || []).map((t) => ({ content: t, disabled: true }));
          new LiteGraph.ContextMenu(items, { event, title: "warnings", className: "dark" });
          break;
        }
      }
      return true;
    },
  };
}

async function loadTemplates(node) {
  try {
    const resp = await api.fetchApi("/promptdeck/cue/templates");
    const data = await resp.json();
    const map = {};
    for (const t of data.templates || []) map[t.name] = t;
    node._cue.templates = map;
  } catch {
    node._cue.templates = {};
  }
  refresh(node);
}

function setupNode(node) {
  node._cue = { templates: {}, idx: 0, info: "", warnings: [], stage: "", streaming: false, pull: null, hoverPos: null };

  // seed defaults to fixed: re-queue keeps the prompt, ↻ rerolls
  const ctrl = node.widgets?.find((w) => w.name === "control_after_generate");
  if (ctrl && !node._cueConfigured && ctrl.value === "randomize") ctrl.value = "fixed";

  for (const name of ["target", "mode"]) {
    const w = widget(node, name);
    if (!w) continue;
    const cb = w.callback;
    w.callback = function () {
      cb?.apply(this, arguments);
      refresh(node);
    };
  }

  // models ▾ right after the writer field
  const btn = node.addWidget("button", "models ▾", null, (value, canvas, btnNode, pos, event) => pickModel(node, event));
  btn.serializeValue = () => null;
  const wi = node.widgets.indexOf(widget(node, "writer"));
  if (wi >= 0) {
    node.widgets.splice(node.widgets.indexOf(btn), 1);
    node.widgets.splice(wi + 1, 0, btn);
  }

  node.addCustomWidget(makeBarWidget(node));
  setVisible(widget(node, "history"), false);
  setVisible(widget(node, "lock"), false);

  const onMouseMove = node.onMouseMove;
  node.onMouseMove = function (e, pos) {
    onMouseMove?.apply(this, arguments);
    this._cue.hoverPos = [pos[0], pos[1]];
    this.setDirtyCanvas(true, false);
  };
  const onMouseLeave = node.onMouseLeave;
  node.onMouseLeave = function () {
    onMouseLeave?.apply(this, arguments);
    this._cue.hoverPos = null;
    this.setDirtyCanvas(true, false);
  };
  const onConnectionsChange = node.onConnectionsChange;
  node.onConnectionsChange = function () {
    onConnectionsChange?.apply(this, arguments);
    refresh(node);
  };

  node.setSize([Math.max(node.size[0], 420), node.size[1]]);
  loadTemplates(node);
  const items = readHistory(node);
  node._cue.idx = Math.max(0, items.length - 1);
  if (items.length) {
    node._cue.info = items[items.length - 1].info || "";
    node._cue.warnings = items[items.length - 1].warnings || [];
  }
}

function findNode(id) {
  return app.graph?.getNodeById?.(parseInt(id, 10)) || app.graph?._nodes?.find((n) => String(n.id) === String(id));
}

app.registerExtension({
  name: "PromptDeck.Cue",
  setup() {
    api.addEventListener("promptdeck.cue", (e) => {
      const d = e.detail || {};
      const node = findNode(d.node);
      if (!node?._cue) return;
      const c = node._cue;
      if (d.event === "stage") {
        c.stage = d.text || "";
        c.streaming = true;
      } else if (d.event === "chunk") {
        c.stage = "writing";
        c.streaming = true;
        const res = widget(node, "result");
        if (res && !widget(node, "lock")?.value) res.value = d.text || "";
      } else if (d.event === "done") {
        onDone(node, d);
      }
      node.setDirtyCanvas(true, false);
    });
    api.addEventListener("promptdeck.cue.pull", (e) => {
      const d = e.detail || {};
      const node = findNode(d.node);
      if (!node?._cue) return;
      const c = node._cue;
      c.pull = { model: d.model, status: d.status || "", completed: d.completed || 0, total: d.total || 0, done: !!d.done, error: d.error };
      if (d.done && !d.error) {
        const w = widget(node, "writer");
        if (w) w.value = d.model;
        setTimeout(() => {
          if (c.pull?.done) c.pull = null;
          node.setDirtyCanvas(true, false);
        }, 4000);
      }
      node.setDirtyCanvas(true, false);
    });
  },
  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData.name !== "PromptDeckCue") return;

    const onNodeCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      onNodeCreated?.apply(this, arguments);
      setupNode(this);
    };

    const onConfigure = nodeType.prototype.onConfigure;
    nodeType.prototype.onConfigure = function (info) {
      this._cueConfigured = true;
      onConfigure?.apply(this, arguments);
      if (this._cue) {
        const items = readHistory(this);
        this._cue.idx = Math.max(0, items.length - 1);
        if (items.length) {
          this._cue.info = items[items.length - 1].info || "";
          this._cue.warnings = items[items.length - 1].warnings || [];
        }
        refresh(this);
      }
    };

    const onExecuted = nodeType.prototype.onExecuted;
    nodeType.prototype.onExecuted = function (message) {
      onExecuted?.apply(this, arguments);
      const d = message?.cue?.[0];
      if (d && this._cue) onDone(this, d);
    };
  },
});
