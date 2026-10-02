import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const HISTORY_DEFAULT = 10;
const HISTORY_OPTIONS = [5, 10, 20, 50];
const BAR_H = 22;
const PAD = 4; // bar margins
const IMG_PAD = 1; // image goes (almost) edge to edge
const BTN_W = 22;
const SAVE_AS_W = 62;
const ALL_W = 30;
const MIN_IMG_H = 80;
const DBLCLICK_MS = 400;

const COLORS = {
  imgBg: "#16161c",
  btn: "#2a2a33",
  btnHover: "#3a3a46",
  btnActive: "#7a3b3b",
  text: "#c8c8d0",
  textDim: "#55555e",
  saved: "#3f9b6a",
  error: "#c05555",
};

function widget(node, name) {
  return node.widgets?.find((w) => w.name === name);
}

function historySize(node) {
  const n = parseInt(node.properties?.keeper_history, 10);
  return HISTORY_OPTIONS.includes(n) ? n : HISTORY_DEFAULT;
}

// Stock Save Image resolves %date:...% / %Node.widget% in the frontend at queue
// time, but only for its own node types — so we do it for ours.
function resolvePrefix(value) {
  const fn = window.comfyAPI?.utils?.applyTextReplacements;
  if (!fn) return value;
  try {
    return fn(app.graph, value);
  } catch {
    try {
      return fn(app, value); // older frontends took the app
    } catch {
      return value;
    }
  }
}

function loadImage(node, item) {
  const img = new Image();
  img.onload = () => node.setDirtyCanvas(true, false);
  img.onerror = () => {
    item.failed = true;
    node.setDirtyCanvas(true, false);
  };
  const q = new URLSearchParams({ filename: item.filename, subfolder: item.subfolder, type: item.type });
  img.src = api.apiURL(`/view?${q}`);
  item.img = img;
}

// Only the current image and its neighbours stay decoded; 50 full-size
// 9:16 previews would otherwise eat a laptop's RAM. Dropped ones reload
// from temp when browsed back to.
function ensureLoaded(node) {
  const k = node._keep;
  k.items.forEach((item, i) => {
    const near = Math.abs(i - k.idx) <= 1;
    if (near && !item.img && !item.failed) loadImage(node, item);
    else if (!near && item.img) item.img = null;
  });
}

function step(node, delta) {
  const k = node._keep;
  if (!k.items.length) return;
  k.idx = Math.max(0, Math.min(k.items.length - 1, k.idx + delta));
  k.error = null;
  ensureLoaded(node);
  node.setDirtyCanvas(true, false);
}

function trimHistory(node) {
  const k = node._keep;
  const drop = Math.max(0, k.items.length - historySize(node));
  if (!drop) return;
  k.items.splice(0, drop);
  k.idx = Math.max(0, k.idx - drop);
}

async function saveCurrent(node, ask) {
  const k = node._keep;
  const item = k.items[k.idx];
  if (!item || k.saving || item.failed) return;
  if (item.saved && !ask) return; // 💾 is one-shot per image; Save as… still works

  let name = "";
  if (ask) {
    const suggestion = item.prefix || "ComfyUI";
    const dialog = app.extensionManager?.dialog;
    name = dialog?.prompt
      ? await dialog.prompt({ title: "Save as", message: "Name (inside the output folder):", defaultValue: suggestion })
      : window.prompt("Save as (inside the output folder):", suggestion);
    name = (name ?? "").trim();
    if (!name) return;
  }

  k.saving = true;
  k.error = null;
  node.setDirtyCanvas(true, false);
  try {
    const resp = await api.fetchApi("/promptdeck/keeper/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        filename: item.filename, subfolder: item.subfolder, type: item.type, prefix: item.prefix, name,
      }),
    });
    const data = await resp.json();
    if (data.ok) item.saved = data.saved;
    else k.error = data.error || "save failed";
  } catch {
    k.error = "save failed";
  }
  k.saving = false;
  node.setDirtyCanvas(true, false);
}

function pickHistorySize(node, event) {
  const current = historySize(node);
  const items = HISTORY_OPTIONS.map((n) => ({
    content: `${n === current ? "● " : "   "}last ${n}`,
    callback: () => {
      node.properties.keeper_history = n;
      trimHistory(node);
      ensureLoaded(node);
      node.setDirtyCanvas(true, true);
    },
  }));
  new LiteGraph.ContextMenu(items, { event, title: "history", className: "dark" });
}

function drawButton(ctx, x, y, w, h, label, enabled, hover, active) {
  ctx.fillStyle = active ? COLORS.btnActive : enabled && hover ? COLORS.btnHover : COLORS.btn;
  ctx.beginPath();
  ctx.roundRect(x, y, w, h, 4);
  ctx.fill();
  ctx.fillStyle = enabled ? COLORS.text : COLORS.textDim;
  ctx.textAlign = "center";
  ctx.fillText(label, x + w / 2, y + h / 2 + 0.5);
}

function makeViewerWidget(node) {
  return {
    name: "keeper",
    type: "PROMPTDECK_KEEPER",
    value: null,
    serializeValue: () => null,
    hits: [],
    imgRect: null,
    lastClick: 0,

    computeSize(width) {
      return [width ?? 300, MIN_IMG_H + BAR_H + PAD * 2];
    },

    draw(ctx, drawNode, width, y) {
      const k = node._keep;
      if (!k) return;
      // take every pixel below the prefix widget: image on top, slim bar under it
      const barY = Math.max(y + MIN_IMG_H + PAD, node.size[1] - BAR_H - PAD);
      const imgX = IMG_PAD;
      const imgY = y;
      const imgW = width - IMG_PAD * 2;
      const imgH = barY - PAD - imgY;
      // keep the frontend's click hit-test in sync with what we actually draw
      this.computedHeight = barY + BAR_H + PAD - y;
      this.hits = [];
      this.imgRect = { x: imgX, y: imgY, w: imgW, h: imgH };

      const item = k.items[k.idx];
      if (item && !item.img && !item.failed) ensureLoaded(node);
      ctx.save();
      ctx.font = "12px sans-serif";
      ctx.textBaseline = "middle";

      // ---- image, contain-fit like Save Image ----
      const img = item?.img;
      if (img?.complete && img.naturalWidth && !item.failed) {
        const s = Math.min(imgW / img.naturalWidth, imgH / img.naturalHeight);
        const dw = img.naturalWidth * s;
        const dh = img.naturalHeight * s;
        const dx = imgX + (imgW - dw) / 2;
        const dy = imgY + (imgH - dh) / 2;
        ctx.drawImage(img, dx, dy, dw, dh);
        if (item.saved) {
          // the one thing allowed on top of the image
          const r = 9;
          const cx = dx + dw - r - 6;
          const cy = dy + r + 6;
          ctx.fillStyle = COLORS.saved;
          ctx.beginPath();
          ctx.arc(cx, cy, r, 0, Math.PI * 2);
          ctx.fill();
          ctx.fillStyle = "#fff";
          ctx.textAlign = "center";
          ctx.fillText("✓", cx, cy + 0.5);
        }
      } else {
        ctx.fillStyle = COLORS.imgBg;
        ctx.beginPath();
        ctx.roundRect(imgX, imgY, imgW, imgH, 4);
        ctx.fill();
        ctx.fillStyle = COLORS.textDim;
        ctx.textAlign = "center";
        const msg = !item ? "previews land here" : item.failed ? "preview gone (temp cleared)" : "loading…";
        ctx.fillText(msg, imgX + imgW / 2, imgY + imgH / 2);
      }

      // ---- slim bar: ◀ n/N ▶ ........ all  Save as…  💾 ----
      const hp = k.hoverPos;
      const over = (x, w) => hp && hp[0] >= x && hp[0] <= x + w && hp[1] >= barY && hp[1] <= barY + BAR_H;
      const n = k.items.length;
      const canPrev = k.idx > 0;
      const canNext = k.idx < n - 1;

      let x = PAD;
      drawButton(ctx, x, barY, BTN_W, BAR_H, "◀", canPrev, over(x, BTN_W));
      this.hits.push({ id: "prev", x, w: BTN_W });
      x += BTN_W;
      const counter = n ? `${k.idx + 1}/${n}` : "0/0";
      const counterW = ctx.measureText("50/50").width + 14;
      ctx.fillStyle = over(x, counterW) ? COLORS.text : n ? COLORS.text : COLORS.textDim;
      ctx.textAlign = "center";
      ctx.fillText(counter, x + counterW / 2, barY + BAR_H / 2 + 0.5);
      this.hits.push({ id: "count", x, w: counterW }); // click → history size menu
      x += counterW;
      drawButton(ctx, x, barY, BTN_W, BAR_H, "▶", canNext, over(x, BTN_W));
      this.hits.push({ id: "next", x, w: BTN_W });
      x += BTN_W;

      const canSave = !!item && !item.failed && !k.saving;
      const keepAll = !!widget(node, "keep_all")?.value;
      let rx = width - PAD - BTN_W - 2;
      drawButton(ctx, rx, barY, BTN_W + 2, BAR_H, k.saving ? "…" : "💾", canSave && !item.saved, over(rx, BTN_W + 2));
      this.hits.push({ id: "save", x: rx, w: BTN_W + 2 });
      rx -= SAVE_AS_W + 4;
      drawButton(ctx, rx, barY, SAVE_AS_W, BAR_H, "Save as…", canSave, over(rx, SAVE_AS_W));
      this.hits.push({ id: "saveas", x: rx, w: SAVE_AS_W });
      rx -= ALL_W + 4;
      drawButton(ctx, rx, barY, ALL_W, BAR_H, "all", true, over(rx, ALL_W), keepAll);
      this.hits.push({ id: "all", x: rx, w: ALL_W });

      // failures show inline in the gap, cleared by the next action
      if (k.error) {
        ctx.fillStyle = COLORS.error;
        ctx.textAlign = "left";
        const maxW = rx - x - 12;
        let msg = k.error;
        while (msg.length > 3 && ctx.measureText(msg).width > maxW) msg = msg.slice(0, -2);
        if (msg !== k.error) msg = msg.slice(0, -1) + "…";
        if (maxW > 20) ctx.fillText(msg, x + 8, barY + BAR_H / 2 + 0.5);
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
      if (!hit) {
        // double-click on the image saves it (the bar is hard to reach when
        // zoomed in on a laptop); a single click falls through so the node still drags
        const r = this.imgRect;
        const onImage = r && px >= r.x && px <= r.x + r.w && py >= r.y && py <= r.y + r.h;
        const now = performance.now();
        const dbl = onImage && now - this.lastClick < DBLCLICK_MS;
        this.lastClick = onImage ? now : 0;
        if (!dbl) return false;
        this.lastClick = 0;
        saveCurrent(node, false);
        return true;
      }
      switch (hit.id) {
        case "prev":
          step(node, -1);
          break;
        case "next":
          step(node, 1);
          break;
        case "count":
          pickHistorySize(node, event);
          break;
        case "save":
          saveCurrent(node, false);
          break;
        case "saveas":
          saveCurrent(node, true);
          break;
        case "all": {
          const w = widget(node, "keep_all");
          if (w) w.value = !w.value;
          node.setDirtyCanvas(true, true);
          break;
        }
      }
      return true;
    },
  };
}

// while a Keeper node is selected (and you're not typing): ←/→ browse, S saves
function onKeyDown(e) {
  const isArrow = e.key === "ArrowLeft" || e.key === "ArrowRight";
  const isSave = e.key === "s" || e.key === "S";
  if (!isArrow && !isSave) return;
  if (e.ctrlKey || e.metaKey || e.altKey || e.shiftKey) return;
  const t = e.target;
  if (t?.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(t?.tagName)) return;
  const selected = Object.values(app.canvas?.selected_nodes ?? {}).filter((n) => n._keep);
  if (!selected.length) return;
  e.preventDefault();
  e.stopPropagation();
  for (const node of selected) {
    if (isArrow) step(node, e.key === "ArrowLeft" ? -1 : 1);
    else saveCurrent(node, false);
  }
}

function setupNode(node) {
  node._keep = { items: [], idx: 0, saving: false, error: null, hoverPos: null };
  if (!node.properties) node.properties = {};
  if (!node.properties.keeper_history) node.properties.keeper_history = HISTORY_DEFAULT;

  const fp = widget(node, "filename_prefix");
  if (fp) fp.serializeValue = () => resolvePrefix(fp.value);

  // keep_all lives in the bar as the "all" toggle, not as a widget row
  const ka = widget(node, "keep_all");
  if (ka) {
    ka.computeSize = () => [0, -4];
    ka.hidden = true;
  }

  const onMouseMove = node.onMouseMove;
  node.onMouseMove = function (e, pos) {
    onMouseMove?.apply(this, arguments);
    this._keep.hoverPos = [pos[0], pos[1]];
    this.setDirtyCanvas(true, false);
  };
  const onMouseLeave = node.onMouseLeave;
  node.onMouseLeave = function () {
    onMouseLeave?.apply(this, arguments);
    this._keep.hoverPos = null;
    this.setDirtyCanvas(true, false);
  };

  node.addCustomWidget(makeViewerWidget(node));
  node.setSize([Math.max(node.size[0], 340), Math.max(node.size[1], 420)]);
}

app.registerExtension({
  name: "PromptDeck.Keeper",
  setup() {
    window.addEventListener("keydown", onKeyDown, { capture: true });
  },
  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData.name !== "PromptDeckKeeper") return;

    const onNodeCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      onNodeCreated?.apply(this, arguments);
      setupNode(this);
    };

    const onExecuted = nodeType.prototype.onExecuted;
    nodeType.prototype.onExecuted = function (message) {
      onExecuted?.apply(this, arguments);
      const fresh = message?.keeper_images;
      const k = this._keep;
      if (!fresh?.length || !k) return;
      for (const f of fresh) {
        // "all" mode saves server-side at generation time → arrives already ✓
        k.items.push({ ...f, saved: f.saved ?? null, failed: false, img: null });
      }
      const limit = historySize(this);
      // land on the first image of the new batch, so ▶ walks through it
      k.idx = Math.max(0, k.items.length - Math.min(fresh.length, limit));
      trimHistory(this);
      ensureLoaded(this);
      k.error = null;
      this.setDirtyCanvas(true, true);
    };
  },
});
