import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const ROW_H = 20;
const HEADER_H = 26;
const PAD = 6;
const CELL = 15; // width of checkbox / star / cross cells
const SCROLLBAR_W = 8;
const MIN_ROWS = 3;
const DEFAULT_ROWS = 7;

// keep in sync with _NUMBERING_RE in prompt_deck.py
const NUMBERING_RE = /^\s*\d{1,4}(?:\s*[.):]|\s+-)\s*/;

const COLORS = {
  listBg: "#16161c",
  headerBtn: "#2a2a33",
  headerBtnActive: "#7a3b3b",
  text: "#c8c8d0",
  textDim: "#55555e",
  num: "#6e6e7a",
  running: "#2c4a3c",
  runningText: "#eafff2",
  next: "#3b6ea5",
  good: "#e8c256",
  bad: "#c05555",
  scrollbar: "#33333d",
  scrollThumb: "#55555f",
};

function widget(node, name) {
  return node.widgets?.find((w) => w.name === name);
}

function parseDisabled(str) {
  const s = new Set();
  for (const p of String(str ?? "").split(/[,;]/)) {
    const n = parseInt(p.trim(), 10);
    if (!isNaN(n)) s.add(n);
  }
  return s;
}

function ratingOk(rating, filter) {
  if (filter === "good only") return rating === "good";
  if (filter === "hide bad") return rating !== "bad";
  return true;
}

// Build the full view model from widgets + fetched lines. Mirrors the Python logic.
function deckInfo(node) {
  const deck = node._deck;
  const disabled = parseDisabled(widget(node, "disabled_lines")?.value);
  const filter = widget(node, "rating_filter")?.value ?? "all";
  const entries = deck.lines.map((raw, i) => {
    const n = i + 1;
    const stripped = raw.trim();
    const blank = !stripped || stripped.startsWith("#");
    const rating = deck.ratings[stripped] ?? "";
    const runnable = !blank && !disabled.has(n) && ratingOk(rating, filter);
    return { n, raw, disp: stripped.replace(NUMBERING_RE, ""), blank, rating, disabled: disabled.has(n), runnable };
  });
  const runnable = entries.filter((e) => e.runnable);
  const idx = widget(node, "line_index")?.value ?? 0;
  const L = runnable.length;
  const ordinal = L ? ((idx % L) + L) % L : 0;
  const nextLine = L ? runnable[ordinal].n : -1;
  const runningLine = deck.lastRun?.line ?? -1;
  return { entries, runnable, ordinal, nextLine, runningLine };
}

async function fetchLines(node) {
  const path = widget(node, "file_path")?.value?.trim();
  const deck = node._deck;
  // line numbers are meaningless across files — drop disables on a real file
  // change (but not on the first fetch after create/configure)
  if (deck.currentPath !== undefined && deck.currentPath !== path) {
    const dl = widget(node, "disabled_lines");
    if (dl) dl.value = "";
    deck.lastRun = null;
  }
  deck.currentPath = path;
  deck.lines = [];
  deck.ratings = {};
  deck.error = path ? null : "no file";
  if (path) {
    try {
      const resp = await api.fetchApi("/promptdeck/read_file", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path }),
      });
      const data = await resp.json();
      if (data.ok) {
        deck.lines = data.lines;
        deck.ratings = data.ratings ?? {};
      } else {
        deck.error = "file not found";
      }
    } catch (e) {
      deck.error = "read failed";
    }
  }
  deck.scroll = null; // back to auto-follow
  node.setDirtyCanvas(true, true);
}

async function sendRating(node, entry) {
  const path = widget(node, "file_path")?.value?.trim();
  if (!path) return;
  try {
    await api.fetchApi("/promptdeck/rate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path, key: entry.raw.trim(), rating: node._deck.ratings[entry.raw.trim()] ?? "" }),
    });
  } catch (e) {
    // rating stays local-only if the write fails; next reload shows the truth
  }
}

function controlWidget(node) {
  return node.widgets?.find((w) => w.name === "control_after_generate");
}

function makeDeckWidget(node) {
  return {
    name: "deck",
    type: "PROMPTDECK",
    value: null,
    serializeValue: () => null,
    hits: [],

    // minimum size only — draw() expands the list to fill the node, so
    // dragging the node taller grows the list (not the text boxes)
    computeSize(width) {
      return [width ?? 320, HEADER_H + MIN_ROWS * ROW_H + PAD];
    },

    draw(ctx, drawNode, width, y) {
      const deck = node._deck;
      if (!deck) return;
      const info = deckInfo(node);
      // fill all remaining node height below this widget
      const avail = node.size[1] - y - HEADER_H - PAD;
      const rows = Math.max(MIN_ROWS, Math.floor(avail / ROW_H));
      deck.rows = rows;
      // computeSize reports only the 3-row minimum (so the node can shrink);
      // the frontend's click hit-test uses computedHeight, so keep it synced
      // to what we actually draw — otherwise clicks below the minimum height
      // fall through and drag the node
      this.computedHeight = HEADER_H + rows * ROW_H + PAD;
      const listX = PAD;
      const listW = width - PAD * 2;
      const listY = y + HEADER_H;
      const listH = rows * ROW_H;
      this.hits = [];

      ctx.save();
      ctx.font = "11px monospace";
      ctx.textBaseline = "middle";

      // ---- header: transport buttons ----
      const held = deck.hold;
      const btns = [
        { id: "prev", label: "◀" },
        { id: "next", label: "▶" },
        { id: "hold", label: held ? "HOLD" : "hold" },
        { id: "reload", label: "↻" },
      ];
      let bx = PAD;
      for (const b of btns) {
        const bw = Math.max(22, ctx.measureText(b.label).width + 12);
        ctx.fillStyle = b.id === "hold" && held ? COLORS.headerBtnActive : COLORS.headerBtn;
        ctx.beginPath();
        ctx.roundRect(bx, y + 3, bw, HEADER_H - 8, 4);
        ctx.fill();
        ctx.fillStyle = COLORS.text;
        ctx.textAlign = "center";
        ctx.fillText(b.label, bx + bw / 2, y + HEADER_H / 2 - 1);
        this.hits.push({ x: bx, y: 3, w: bw, h: HEADER_H - 8, id: b.id });
        bx += bw + 4;
      }

      // ---- header: position counter + word count, right aligned ----
      ctx.textAlign = "right";
      ctx.fillStyle = COLORS.num;
      let posLabel;
      if (info.runnable.length) {
        // word count of the last composed prompt, or of the upcoming line
        const current = widget(node, "resolved")?.value?.trim();
        const basis = current || info.runnable[info.ordinal]?.disp || "";
        const words = basis ? basis.split(/\s+/).length : 0;
        posLabel = `${info.ordinal + 1} / ${info.runnable.length} · ${words}w`;
      } else {
        posLabel = deck.error ?? "empty";
      }
      ctx.fillText(posLabel, width - PAD, y + HEADER_H / 2 - 1);

      // ---- list background ----
      ctx.fillStyle = COLORS.listBg;
      ctx.beginPath();
      ctx.roundRect(listX, listY, listW, listH, 4);
      ctx.fill();

      const entries = info.entries;
      if (!entries.length) {
        ctx.fillStyle = COLORS.textDim;
        ctx.textAlign = "center";
        ctx.fillText(deck.error ?? "no prompts loaded", width / 2, listY + listH / 2);
        ctx.restore();
        return;
      }

      // ---- scroll position: null => auto-follow running (or next) line ----
      const followLine = info.runningLine > 0 ? info.runningLine : info.nextLine;
      let top;
      if (deck.scroll == null) {
        top = Math.round(followLine - 1 - (rows - 1) / 2);
      } else {
        top = deck.scroll;
      }
      top = Math.max(0, Math.min(top, entries.length - rows));
      if (deck.scroll != null) deck.scroll = top; // keep manual scroll in bounds
      deck.top = top;

      // ---- rows ----
      ctx.beginPath();
      ctx.rect(listX, listY, listW, listH);
      ctx.clip();

      const numW = ctx.measureText(String(entries.length)).width + 6;
      const cellsW = CELL * 3;
      const textX = listX + cellsW + numW + 10;
      const textMaxW = listX + listW - SCROLLBAR_W - 6 - textX;

      for (let r = 0; r < rows; r++) {
        const e = entries[top + r];
        if (!e) break;
        const ry = listY + r * ROW_H;
        const cy = ry + ROW_H / 2;
        const isRunning = e.n === info.runningLine;
        const isNext = e.n === info.nextLine;

        if (isRunning) {
          ctx.fillStyle = COLORS.running;
          ctx.beginPath();
          ctx.roundRect(listX + 2, ry + 1, listW - SCROLLBAR_W - 4, ROW_H - 2, 3);
          ctx.fill();
        }
        if (isNext && !isRunning) {
          ctx.fillStyle = COLORS.next;
          ctx.fillRect(listX + 2, ry + 3, 3, ROW_H - 6);
        }

        let cx = listX + 4;
        if (!e.blank) {
          // enable checkbox
          ctx.strokeStyle = COLORS.textDim;
          ctx.lineWidth = 1;
          ctx.strokeRect(cx + 2, cy - 5, 10, 10);
          if (!e.disabled) {
            ctx.fillStyle = "#6f9b7d";
            ctx.fillRect(cx + 4, cy - 3, 6, 6);
          }
          this.hits.push({ x: cx, y: ry - y, w: CELL, h: ROW_H, id: "toggle", n: e.n });
          cx += CELL;

          // rating: star
          ctx.textAlign = "center";
          ctx.fillStyle = e.rating === "good" ? COLORS.good : COLORS.textDim;
          ctx.fillText(e.rating === "good" ? "★" : "☆", cx + CELL / 2, cy);
          this.hits.push({ x: cx, y: ry - y, w: CELL, h: ROW_H, id: "good", n: e.n });
          cx += CELL;

          // rating: cross
          ctx.fillStyle = e.rating === "bad" ? COLORS.bad : COLORS.textDim;
          ctx.fillText("✕", cx + CELL / 2, cy);
          this.hits.push({ x: cx, y: ry - y, w: CELL, h: ROW_H, id: "bad", n: e.n });
        }

        // line number, right aligned in its gutter
        ctx.textAlign = "right";
        ctx.fillStyle = isRunning ? COLORS.runningText : COLORS.num;
        ctx.fillText(String(e.n), listX + cellsW + numW + 4, cy);

        // prompt text
        ctx.textAlign = "left";
        if (e.blank) ctx.fillStyle = COLORS.textDim;
        else if (isRunning) ctx.fillStyle = COLORS.runningText;
        else if (!e.runnable) ctx.fillStyle = COLORS.textDim;
        else ctx.fillStyle = COLORS.text;
        let t = e.blank ? e.raw.trim() : e.disp;
        if (ctx.measureText(t).width > textMaxW) {
          while (t.length > 1 && ctx.measureText(t + "…").width > textMaxW) {
            t = t.slice(0, -4);
          }
          t += "…";
        }
        ctx.fillText(t, textX, cy);
        if (!e.blank) {
          // jump zone stops before the scrollbar so it can't swallow its clicks
          this.hits.push({
            x: listX + cellsW + CELL, y: ry - y,
            w: listW - cellsW - CELL - SCROLLBAR_W - 6, h: ROW_H,
            id: "jump", n: e.n,
          });
        }
      }

      // ---- scrollbar ----
      deck.sb = null;
      if (entries.length > rows) {
        const sbX = listX + listW - SCROLLBAR_W - 2;
        const trackY = listY + 2;
        const trackH = listH - 4;
        ctx.fillStyle = COLORS.scrollbar;
        ctx.beginPath();
        ctx.roundRect(sbX, trackY, SCROLLBAR_W, trackH, 3);
        ctx.fill();
        const maxTop = entries.length - rows;
        const thumbH = Math.max(12, (rows / entries.length) * trackH);
        const thumbY = trackY + (top / maxTop) * (trackH - thumbH);
        ctx.fillStyle = deck.sbDrag ? "#7a7a88" : COLORS.scrollThumb;
        ctx.beginPath();
        ctx.roundRect(sbX, thumbY, SCROLLBAR_W, thumbH, 3);
        ctx.fill();
        // widget-relative geometry for hit-testing and drag math
        deck.sb = {
          x: sbX - 4, w: SCROLLBAR_W + 8,
          trackY: trackY - y, trackH,
          thumbY: thumbY - y, thumbH, maxTop,
        };
      }

      // ---- hover tooltip: full prompt text for the row under the cursor ----
      const hp = deck.hoverPos;
      if (hp && !deck.sbDrag && hp[0] >= listX && hp[0] <= listX + listW - SCROLLBAR_W) {
        const row = Math.floor((hp[1] - listY) / ROW_H);
        const e = row >= 0 && row < rows ? entries[top + row] : null;
        if (e && !e.blank && ctx.measureText(e.disp).width > textMaxW) {
          const tipMaxW = listW - 24;
          // word-wrap the full text
          const words = e.disp.split(/\s+/);
          const lines = [];
          let cur = "";
          for (const w of words) {
            const cand = cur ? cur + " " + w : w;
            if (ctx.measureText(cand).width > tipMaxW && cur) {
              lines.push(cur);
              cur = w;
            } else {
              cur = cand;
            }
          }
          if (cur) lines.push(cur);
          const tipH = lines.length * (ROW_H - 4) + 10;
          const tipW = Math.min(tipMaxW, Math.max(...lines.map((l) => ctx.measureText(l).width))) + 16;
          const rowY = listY + row * ROW_H;
          let ty = rowY + ROW_H + 2;
          if (ty + tipH > listY + listH) ty = rowY - tipH - 2;
          const tx = listX + 8;
          ctx.fillStyle = "#26262e";
          ctx.strokeStyle = "#4a4a55";
          ctx.lineWidth = 1;
          ctx.beginPath();
          ctx.roundRect(tx, ty, tipW, tipH, 5);
          ctx.fill();
          ctx.stroke();
          ctx.fillStyle = "#e0e0e6";
          ctx.textAlign = "left";
          for (let i = 0; i < lines.length; i++) {
            ctx.fillText(lines[i], tx + 8, ty + 5 + i * (ROW_H - 4) + (ROW_H - 4) / 2);
          }
        }
      }

      ctx.restore();
    },

    mouse(event, pos, mouseNode) {
      const deck = node._deck;
      if (!deck) return false;
      const lx = pos[0];
      const ly = pos[1] - (this.last_y ?? 0);

      // ---- scrollbar thumb dragging ----
      if (event.type === "pointermove" || event.type === "mousemove") {
        if (deck.sbDrag && deck.sb) {
          const t = (ly - deck.sbDrag.grab - deck.sb.trackY) / (deck.sb.trackH - deck.sb.thumbH);
          deck.scroll = Math.round(Math.max(0, Math.min(1, t)) * deck.sb.maxTop);
          node.setDirtyCanvas(true, false);
          return true;
        }
        return false;
      }
      if (event.type === "pointerup" || event.type === "mouseup") {
        if (deck.sbDrag) {
          deck.sbDrag = null;
          node.setDirtyCanvas(true, false);
          return true;
        }
        return false;
      }
      if (event.type !== "pointerdown" && event.type !== "mousedown") return false;

      // ---- scrollbar first, so list rows can never swallow its clicks ----
      if (deck.sb && lx >= deck.sb.x && lx <= deck.sb.x + deck.sb.w &&
          ly >= deck.sb.trackY && ly <= deck.sb.trackY + deck.sb.trackH) {
        if (ly >= deck.sb.thumbY && ly <= deck.sb.thumbY + deck.sb.thumbH) {
          deck.sbDrag = { grab: ly - deck.sb.thumbY };
        } else if (ly < deck.sb.thumbY) {
          deck.scroll = (deck.top ?? 0) - deck.rows;
        } else {
          deck.scroll = (deck.top ?? 0) + deck.rows;
        }
        node.setDirtyCanvas(true, true);
        return true;
      }

      const hit = this.hits.find((h) => lx >= h.x && lx <= h.x + h.w && ly >= h.y && ly <= h.y + h.h);
      if (!hit) return false;

      const info = deckInfo(node);
      const li = widget(node, "line_index");
      const L = info.runnable.length;

      switch (hit.id) {
        case "prev":
          if (L && li) li.value = (info.ordinal - 1 + L) % L;
          deck.scroll = null;
          break;
        case "next":
          if (L && li) li.value = (info.ordinal + 1) % L;
          deck.scroll = null;
          break;
        case "hold": {
          deck.hold = !deck.hold;
          const ctrl = controlWidget(node);
          if (ctrl) {
            if (deck.hold) {
              deck.prevCtrl = ctrl.value;
              ctrl.value = "fixed";
            } else {
              ctrl.value = deck.prevCtrl ?? "increment";
            }
          }
          break;
        }
        case "reload":
          fetchLines(node);
          break;
        case "toggle": {
          const dl = widget(node, "disabled_lines");
          if (dl) {
            const s = parseDisabled(dl.value);
            s.has(hit.n) ? s.delete(hit.n) : s.add(hit.n);
            dl.value = [...s].sort((a, b) => a - b).join(",");
          }
          break;
        }
        case "good":
        case "bad": {
          const e = info.entries[hit.n - 1];
          const key = e.raw.trim();
          const target = hit.id === "good" ? "good" : "bad";
          deck.ratings[key] = deck.ratings[key] === target ? "" : target;
          sendRating(node, e);
          break;
        }
        case "jump": {
          const ord = info.runnable.findIndex((e) => e.n === hit.n);
          if (ord >= 0 && li) {
            li.value = ord;
            deck.scroll = null;
          }
          break;
        }
      }
      node.setDirtyCanvas(true, true);
      return true;
    },
  };
}

// ---- mouse wheel over the list scrolls it (with acceleration) instead of zooming ----
function onCanvasWheel(e) {
  const canvas = app.canvas;
  if (!canvas?.graph) return;
  const rect = canvas.canvas.getBoundingClientRect();
  const gx = (e.clientX - rect.left) / canvas.ds.scale - canvas.ds.offset[0];
  const gy = (e.clientY - rect.top) / canvas.ds.scale - canvas.ds.offset[1];
  for (const node of canvas.graph._nodes ?? []) {
    if (node.type !== "PromptDeck" || !node._deck || node.flags?.collapsed) continue;
    const dw = widget(node, "deck");
    if (!dw || dw.last_y == null) continue;
    const lx = gx - node.pos[0];
    const ly = gy - node.pos[1];
    const deck = node._deck;
    const rows = deck.rows ?? MIN_ROWS;
    const listTop = dw.last_y + HEADER_H;
    if (lx < PAD || lx > node.size[0] - PAD || ly < listTop || ly > listTop + rows * ROW_H) continue;
    if ((deck.lines?.length ?? 0) <= rows) return; // nothing to scroll, let canvas zoom

    e.preventDefault();
    e.stopImmediatePropagation();

    // acceleration: single flicks move gently, sustained spinning speeds up
    const now = performance.now();
    deck.wheelStreak = now - (deck.wheelT ?? 0) < 150 ? (deck.wheelStreak ?? 0) + 1 : 0;
    deck.wheelT = now;
    const speed = 1 + Math.min(Math.floor(deck.wheelStreak / 5), 7); // 1x → 8x

    const delta = e.deltaMode === 1 ? e.deltaY * 20 : e.deltaY; // line-mode wheels
    deck.wheelAccum = (deck.wheelAccum ?? 0) + delta;
    const steps = Math.trunc(deck.wheelAccum / 60);
    if (steps) {
      deck.wheelAccum -= steps * 60;
      deck.scroll = (deck.scroll ?? deck.top ?? 0) + steps * speed;
      node.setDirtyCanvas(true, false);
    }
    return;
  }
}

function setupNode(node) {
  node._deck = {
    lines: [],
    ratings: {},
    lastRun: null,
    scroll: null,
    rows: DEFAULT_ROWS,
    hold: false,
    error: null,
  };

  const dl = widget(node, "disabled_lines");
  if (dl) {
    dl.computeSize = () => [0, -4];
    dl.hidden = true;
  }
  const res = widget(node, "resolved");
  if (res?.inputEl) {
    res.inputEl.readOnly = true;
    res.inputEl.style.opacity = "0.75";
    res.inputEl.placeholder = "resolved prompt appears here after each run";
  }
  if (res) {
    // ask the layout engine for a taller slot — never inflate the DOM element
    // directly, or it overlaps the canvas widgets below it
    res.computeSize = function (width) {
      return [width ?? 300, 110];
    };
  }
  // fixed height for pre/after so node resizing feeds the list, not these;
  // long text scrolls inside the boxes
  for (const name of ["pre_text", "after_text"]) {
    const w = widget(node, name);
    if (w) {
      w.computeSize = function (width) {
        return [width ?? 300, 70];
      };
    }
  }
  const fp = widget(node, "file_path");
  if (fp) {
    const cb = fp.callback;
    fp.callback = function (...args) {
      const r = cb?.apply(this, args);
      fetchLines(node);
      return r;
    };
  }
  // recent-files dropdown, right under the path
  const recentBtn = node.addWidget("button", "recent ▾", null, (value, canvas, btnNode, pos, event) => {
    api.fetchApi("/promptdeck/recents")
      .then((r) => r.json())
      .then((data) => {
        const recents = (data.recents ?? []).filter((p) => p !== fp?.value);
        if (!recents.length) return;
        const items = recents.map((p) => {
          const parts = p.split(/[\\/]/).filter(Boolean);
          return {
            content: parts.slice(-2).join("/"),
            title: p,
            callback: () => {
              if (fp) {
                fp.value = p;
                fp.callback?.(p);
                node.setDirtyCanvas(true, true);
              }
            },
          };
        });
        new LiteGraph.ContextMenu(items, { event, title: "recent files", className: "dark" });
      })
      .catch(() => {});
  });
  recentBtn.serializeValue = () => null;
  const btnIdx = node.widgets.indexOf(recentBtn);
  const fpIdx = node.widgets.indexOf(fp);
  if (fp && btnIdx > fpIdx + 1) {
    node.widgets.splice(btnIdx, 1);
    node.widgets.splice(fpIdx + 1, 0, recentBtn);
  }
  // keep the hold button in sync if the user flips the control combo by hand
  const ctrl = controlWidget(node);
  if (ctrl) {
    ctrl.value = "increment"; // forward by default; onConfigure restores saved workflows
    const cb = ctrl.callback;
    ctrl.callback = function (...args) {
      const r = cb?.apply(this, args);
      node._deck.hold = ctrl.value === "fixed";
      node.setDirtyCanvas(true, true);
      return r;
    };
  }

  // hover tracking for the full-prompt tooltip
  const onMouseMove = node.onMouseMove;
  node.onMouseMove = function (e, pos) {
    onMouseMove?.apply(this, arguments);
    if (this._deck) {
      this._deck.hoverPos = [pos[0], pos[1]];
      this.setDirtyCanvas(true, false);
    }
  };
  const onMouseLeave = node.onMouseLeave;
  node.onMouseLeave = function () {
    onMouseLeave?.apply(this, arguments);
    if (this._deck) {
      this._deck.hoverPos = null;
      this.setDirtyCanvas(true, false);
    }
  };

  node.addCustomWidget(makeDeckWidget(node));
  // computeSize only guarantees the 3-row minimum; open fresh nodes at a
  // comfortable width and DEFAULT_ROWS worth of list
  const min = node.computeSize();
  node.setSize([
    Math.max(node.size[0], 360),
    min[1] + (DEFAULT_ROWS - MIN_ROWS) * ROW_H,
  ]);
  fetchLines(node);
}

app.registerExtension({
  name: "PromptDeck.UI",
  setup() {
    app.canvas.canvas.addEventListener("wheel", onCanvasWheel, { capture: true, passive: false });
  },
  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData.name !== "PromptDeck") return;

    const onNodeCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      onNodeCreated?.apply(this, arguments);
      setupNode(this);
    };

    // migrate workflows saved before the "recent ▾" button existed
    // (pre-1.3: 13 widget slots) — insert its slot so values don't shift.
    // Must wrap configure(), not onConfigure(): values are assigned before
    // onConfigure fires.
    const configure = nodeType.prototype.configure;
    nodeType.prototype.configure = function (info) {
      if (Array.isArray(info?.widgets_values) && info.widgets_values.length === 13) {
        info.widgets_values.splice(1, 0, null);
      }
      return configure.apply(this, arguments);
    };

    const onConfigure = nodeType.prototype.onConfigure;
    nodeType.prototype.onConfigure = function () {
      onConfigure?.apply(this, arguments);
      if (this._deck) {
        this._deck.hold = controlWidget(this)?.value === "fixed";
        this._deck.currentPath = undefined; // loading a workflow is not a file change
        fetchLines(this);
      }
    };

    const onExecuted = nodeType.prototype.onExecuted;
    nodeType.prototype.onExecuted = function (message) {
      onExecuted?.apply(this, arguments);
      const s = message?.deck_state?.[0];
      if (!s || !this._deck) return;
      this._deck.lastRun = s;
      this._deck.scroll = null; // follow the running line
      const res = widget(this, "resolved");
      if (res) res.value = s.resolved ?? "";
      this.setDirtyCanvas(true, true);
    };
  },
});
