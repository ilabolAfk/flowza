/* Flowza — клиентский редактор на SVG.
   Канвас (панорама/зум/ноды), рёбра Безье, палитра, инспектор,
   живой прогон через /api/run, сохранение, отмена/повтор, статусы нод.
*/

"use strict";

const state = {
  specs: {},            // type -> spec (каталог)
  graph: { nodes: [], edges: [] },
  byId: new Map(),      // node_id -> node
  selection: new Set(), // выделенные node_id
  active: null,         // нода, показанная в инспекторе
  zoom: 1,
  view: { x: 40, y: 40 },
  panning: null,
  drafting: null,
  dirty: false,
  undo: [],
  redo: [],
  pendingHistory: null,
  historyTimer: null,
  busy: false,
  report: null,
  edgeMenu: null,   // {id, x, y} — выбранная связь и её точка в мировых координатах
  settings: null,
  settingsTab: "run",
  runId: null,
  runTimer: null,
  progress: {},
  autosaveTimer: null,
};

const $ = (s) => document.querySelector(s);
const svg = $("#svg");
const NS = "http://www.w3.org/2000/svg";
const NODE_W = 170, NODE_HEAD = 30, PORT_R = 6, ROW_H = 26;
const MIME = "application/x-flowza-node";
let spaceDown = false;

// ==========================================================================
// Иконки: Font Awesome 5 из локальных файлов (web/vendor/fa), без CDN
// ==========================================================================
const FA = {
  pointer: ["fa-hand-pointer", "\uf25a"],
  clock: ["fa-clock", "\uf017"],
  link: ["fa-link", "\uf0c1"],
  keyboard: ["fa-keyboard", "\uf11c"],
  branch: ["fa-code-branch", "\uf126"],
  filter: ["fa-filter", "\uf0b0"],
  sitemap: ["fa-sitemap", "\uf0e8"],
  stopwatch: ["fa-stopwatch", "\uf2f2"],
  exchange: ["fa-exchange-alt", "\uf362"],
  rename: ["fa-i-cursor", "\uf246"],
  align: ["fa-align-left", "\uf036"],
  calculator: ["fa-calculator", "\uf1ec"],
  envelope: ["fa-envelope", "\uf0e0"],
  robot: ["fa-robot", "\uf544"],
  send: ["fa-paper-plane", "\uf1d8"],
  note: ["fa-sticky-note", "\uf249"],
  eye: ["fa-eye", "\uf06e"],
  globe: ["fa-globe", "\uf0ac"],
  loop: ["fa-sync-alt", "\uf2f1"],
  sigma: ["fa-layer-group", "\uf5fd"],
  code: ["fa-code", "\uf121"],
  calendar: ["fa-calendar-alt", "\uf073"],
  "file-in": ["fa-file-import", "\uf56f"],
  "file-out": ["fa-file-export", "\uf56e"],
  database: ["fa-database", "\uf1c0"],
  cog: ["fa-cog", "\uf013"],
  history: ["fa-history", "\uf1da"],
  dot: ["fa-circle", "\uf111"],
};

function faClass(name) {
  return (FA[name] || FA.dot)[0];
}

function faGlyph(name) {
  return (FA[name] || FA.dot)[1];
}



function el(tag, attrs = {}, parent) {
  const node = document.createElementNS(NS, tag);
  for (const key in attrs) {
    if (Object.prototype.hasOwnProperty.call(attrs, key)) node.setAttribute(key, attrs[key]);
  }
  if (parent) parent.appendChild(node);
  return node;
}

function paletteIcon(name, color) {
  const icon = document.createElement("i");
  icon.className = "fas " + faClass(name);
  icon.style.color = color || "#8fa3b8";
  return icon;
}

function appendNodeIcon(group, name) {
  const text = el("text", { class: "node-ico", x: 15, y: 21, "text-anchor": "middle" }, group);
  text.textContent = faGlyph(name);
}

// ==========================================================================
// Канвас: система координат
// ==========================================================================
function applyView() {
  const t = `translate(${state.view.x},${state.view.y}) scale(${state.zoom})`;
  ["#bg", "#edges", "#nodes", "#draft"].forEach((sel) => {
    const group = $(sel);
    if (group) group.setAttribute("transform", t);
  });
  positionEdgeMenu();
}

function screenToWorld(clientX, clientY) {
  const rect = svg.getBoundingClientRect();
  return {
    x: (clientX - rect.left - state.view.x) / state.zoom,
    y: (clientY - rect.top - state.view.y) / state.zoom,
  };
}

function worldToScreen(wx, wy) {
  const rect = svg.getBoundingClientRect();
  return {
    x: wx * state.zoom + state.view.x + rect.left,
    y: wy * state.zoom + state.view.y + rect.top,
  };
}

// ==========================================================================
// Палитра
// ==========================================================================
async function loadSpecs() {
  const res = await fetch("/api/specs");
  if (!res.ok) throw new Error("не удалось загрузить каталог нод");
  state.specs = await res.json();
  renderPalette();
}

function renderPalette() {
  const cats = {};
  for (const [type, spec] of Object.entries(state.specs)) {
    (cats[spec.category] ||= []).push([type, spec]);
  }
  const box = $("#palette-cats");
  box.innerHTML = "";
  for (const [cat, items] of Object.entries(cats)) {
    const head = document.createElement("div");
    head.className = "pcat-name";
    head.textContent = cat;
    box.appendChild(head);

    const catBox = document.createElement("div");
    catBox.className = "pcat";
    box.appendChild(catBox);

    for (const [type, spec] of items) {
      const item = document.createElement("div");
      item.className = "pnode";
      item.dataset.type = type;
      item.draggable = true;
      item.title = spec.note || spec.title;
      item.appendChild(paletteIcon(spec.icon, spec.color));
      const label = document.createElement("span");
      label.textContent = spec.title;
      item.appendChild(label);

      item.addEventListener("dragstart", (e) => {
        e.dataTransfer.setData(MIME, type);
        e.dataTransfer.effectAllowed = "copy";
      });
      item.addEventListener("click", () => addNodeAtCenter(type));
      item.dataset.search = (cat + " " + spec.title).toLowerCase();
      catBox.appendChild(item);
    }
  }
}

function applyPaletteFilter() {
  const query = $("#palette-input").value.trim().toLowerCase();
  document.querySelectorAll("#palette-cats .pnode").forEach((item) => {
    item.style.display = !query || item.dataset.search.includes(query) ? "" : "none";
  });
  document.querySelectorAll("#palette-cats .pcat").forEach((catBox) => {
    const visible = [...catBox.querySelectorAll(".pnode")].some((n) => n.style.display !== "none");
    catBox.style.display = visible ? "" : "none";
    const head = catBox.previousElementSibling;
    if (head && head.classList.contains("pcat-name")) head.style.display = visible ? "" : "none";
  });
}

// ==========================================================================
// Модель и отрисовка
// ==========================================================================
function hydrateNode(node) {
  const spec = state.specs[node.type] || { inputs: [], outputs: [], params: [], title: node.type };
  node.spec = spec;
  node.title = spec.title || node.type;
  node.inputs = spec.inputs || [];
  node.outputs = spec.outputs || [];
  node.params = Object.assign({}, node.params);
  (spec.params || []).forEach((p) => {
    if (node.params[p.name] === undefined) node.params[p.name] = p.default !== undefined ? p.default : "";
  });
  return node;
}

function reindex() {
  state.byId.clear();
  state.graph.nodes.forEach((node) => {
    hydrateNode(node);
    state.byId.set(node.id, node);
  });
  state.selection = new Set([...state.selection].filter((id) => state.byId.has(id)));
  if (state.active && !state.byId.has(state.active)) state.active = null;
}

function newId(prefix) {
  return prefix + Math.random().toString(36).slice(2, 10);
}

function addNode(type, wx, wy) {
  const spec = state.specs[type];
  if (!spec) { setStatus("Неизвестный тип ноды: " + type, true); return null; }
  pushHistory();
  const defaults = {};
  (spec.params || []).forEach((p) => {
    defaults[p.name] = p.default !== undefined ? p.default : "";
  });
  const node = hydrateNode({
    id: newId("n"), type,
    x: Math.round(wx), y: Math.round(wy),
    params: defaults,
  });
  state.graph.nodes.push(node);
  state.byId.set(node.id, node);
  renderNode(node);
  selectOnly(node.id);
  markDirty();
  return node;
}

function addNodeAtCenter(type) {
  const rect = svg.getBoundingClientRect();
  const point = screenToWorld(rect.left + rect.width / 2, rect.top + rect.height / 2);
  addNode(type, point.x - NODE_W / 2, point.y - 40);
}

function nodeHeight(node) {
  return NODE_HEAD + Math.max(node.inputs.length, node.outputs.length, 1) * ROW_H;
}

function nodeGroup(id) {
  return document.querySelector(`.node-group[data-id="${id}"]`);
}

function fitText(textEl, text, maxWidth) {
  textEl.textContent = text;
  if (textEl.getComputedTextLength() <= maxWidth) return;
  let low = 0, high = text.length;
  while (low < high) {
    const mid = (low + high + 1) >> 1;
    textEl.textContent = text.slice(0, mid) + "…";
    if (textEl.getComputedTextLength() <= maxWidth) low = mid; else high = mid - 1;
  }
  textEl.textContent = text.slice(0, low) + "…";
}

function renderNode(node) {
  const group = el("g", { class: "node-group", "data-id": node.id });
  group.setAttribute("transform", `translate(${node.x},${node.y})`);
  group.style.setProperty("--nodecolor", node.spec.color || "#3b82f6");
  $("#nodes").appendChild(group);
  const height = nodeHeight(node);

  el("rect", { class: "node-body", x: 0, y: 0, width: NODE_W, height, rx: 8 }, group);
  el("path", {
    class: "node-head",
    d: `M0,8 Q0,0 8,0 L${NODE_W - 8},0 Q${NODE_W},0 ${NODE_W},8 L${NODE_W},${NODE_HEAD} L0,${NODE_HEAD} Z`,
  }, group);

  appendNodeIcon(group, node.spec.icon);

  const title = el("text", { class: "node-title", x: 26, y: 21 }, group);
  fitText(title, node.title, NODE_W - 26 - 12);

  node.inputs.forEach((port, index) => {
    const py = NODE_HEAD + (index + 0.5) * ROW_H;
    el("circle", {
      class: "port-circle", cx: 0, cy: py, r: PORT_R,
      "data-node": node.id, "data-port": port.name, "data-dir": "in",
    }, group);
    const label = el("text", { class: "node-label", x: 13, y: py + 3.5 }, group);
    fitText(label, port.label, NODE_W / 2 - 17);
  });

  node.outputs.forEach((port, index) => {
    const py = NODE_HEAD + (index + 0.5) * ROW_H;
    el("circle", {
      class: "port-circle", cx: NODE_W, cy: py, r: PORT_R,
      "data-node": node.id, "data-port": port.name, "data-dir": "out",
    }, group);
    const label = el("text", {
      class: "node-label", x: NODE_W - 13, y: py + 3.5, "text-anchor": "end",
    }, group);
    fitText(label, port.label, NODE_W / 2 - 17);
  });
}

function renderAll() {
  $("#nodes").innerHTML = "";
  $("#edges").innerHTML = "";
  state.graph.nodes.forEach(renderNode);
  renderEdges();
  refreshPortKinds();
  updateSelectionDOM();
  applyStatuses();
}

function renderEdges() {
  const layer = $("#edges");
  layer.innerHTML = "";
  state.graph.edges.forEach((edge) => {
    const src = state.byId.get(edge.source);
    const tgt = state.byId.get(edge.target);
    if (!src || !tgt) return;
    const from = portPos(src, edge.source_port, "out");
    const to = portPos(tgt, edge.target_port, "in");
    if (!from || !to) return;
    const d = bezier(from, to);
    const active = state.edgeMenu && state.edgeMenu.id === edge.id ? " edge-active" : "";
    // видимая линия не перехватывает мышь — иначе по ней невозможно панорамировать
    el("path", {
      class: "edge-path" + active, d,
      "data-edge": edge.id, "data-target": edge.target,
    }, layer).setAttribute("pointer-events", "none");
    // широкая невидимая зона: по ней ловится клик и подсветка
    el("path", {
      class: "edge-hit", d,
      "data-edge": edge.id, "data-target": edge.target,
    }, layer);
  });
}

function portPos(node, portName, dir) {
  const ports = dir === "out" ? node.outputs : node.inputs;
  const index = ports.findIndex((p) => p.name === portName);
  if (index < 0) {
    console.warn("Порт не найден:", node.type, portName, dir);
    return null;
  }
  return {
    x: node.x + (dir === "out" ? NODE_W : 0),
    y: node.y + NODE_HEAD + (index + 0.5) * ROW_H,
  };
}

function bezier(p1, p2) {
  const dx = Math.max(40, Math.abs(p2.x - p1.x) * 0.5);
  return `M ${p1.x},${p1.y} C ${p1.x + dx},${p1.y} ${p2.x - dx},${p2.y} ${p2.x},${p2.y}`;
}

function refreshPortKinds() {
  const linkedIn = new Set(), linkedOut = new Set();
  state.graph.edges.forEach((edge) => {
    linkedOut.add(edge.source + "::" + edge.source_port);
    linkedIn.add(edge.target + "::" + edge.target_port);
  });
  document.querySelectorAll("#nodes circle.port-circle").forEach((circle) => {
    const key = circle.dataset.node + "::" + circle.dataset.port;
    const linked = circle.dataset.dir === "in" ? linkedIn.has(key) : linkedOut.has(key);
    circle.classList.toggle("linked", linked);
  });
}

// ==========================================================================
// Соединения
// ==========================================================================
function addEdge(source, sourcePort, target, targetPort) {
  if (source === target) { setStatus("Ноду нельзя соединить с самой собой", true); return; }
  const targetNode = state.byId.get(target);
  if (targetNode && !targetNode.inputs.some((p) => p.name === targetPort)) return;
  pushHistory();
  state.graph.edges = state.graph.edges.filter(
    (e) => !(e.target === target && e.target_port === targetPort));
  state.graph.edges.push({
    id: newId("e"), source, source_port: sourcePort,
    target, target_port: targetPort,
  });
  renderEdges();
  refreshPortKinds();
  markDirty();
}

function removeEdge(edgeId) {
  if (!state.graph.edges.some((e) => e.id === edgeId)) return;
  closeEdgeMenu();
  pushHistory();
  state.graph.edges = state.graph.edges.filter((e) => e.id !== edgeId);
  renderEdges();
  refreshPortKinds();
  markDirty();
  setStatus("Связь удалена (Ctrl+Z — вернуть)");
}

// --- меню связи -----------------------------------------------------------
function edgeMenuPoint(edge) {
  const src = state.byId.get(edge.source);
  const tgt = state.byId.get(edge.target);
  if (!src || !tgt) return null;
  const from = portPos(src, edge.source_port, "out");
  const to = portPos(tgt, edge.target_port, "in");
  if (!from || !to) return null;
  const dx = Math.max(40, Math.abs(to.x - from.x) * 0.5);
  const c1 = { x: from.x + dx, y: from.y };
  const c2 = { x: to.x - dx, y: to.y };
  // точка кривой при t = 0.5 — меню висит ровно над линией
  return {
    x: 0.125 * from.x + 0.375 * c1.x + 0.375 * c2.x + 0.125 * to.x,
    y: 0.125 * from.y + 0.375 * c1.y + 0.375 * c2.y + 0.125 * to.y,
  };
}

function positionEdgeMenu() {
  if (!state.edgeMenu) return;
  const menu = $("#edge-menu");
  const rect = $("#canvas").getBoundingClientRect();
  const screen = worldToScreen(state.edgeMenu.x, state.edgeMenu.y);
  const top = screen.y - rect.top;
  const below = top < 70;   // у верхней кромки меню раскрывается вниз, иначе обрежется
  menu.classList.toggle("below", below);
  menu.style.left = (screen.x - rect.left) + "px";
  menu.style.top = (below ? top + 12 : top - 12) + "px";
}

function openEdgeMenu(edgeId) {
  const edge = state.graph.edges.find((e) => e.id === edgeId);
  if (!edge) return;
  const point = edgeMenuPoint(edge);
  if (!point) return;
  state.edgeMenu = { id: edgeId, x: point.x, y: point.y };
  renderEdges();
  positionEdgeMenu();
  $("#edge-menu").style.display = "block";
}

function closeEdgeMenu() {
  if (!state.edgeMenu) return;
  state.edgeMenu = null;
  $("#edge-menu").style.display = "none";
  renderEdges();
}

function wirePortEvents() {
  document.addEventListener("mousedown", (e) => {
    if (e.button !== 0) return;
    const circle = e.target.closest && e.target.closest("circle.port-circle");
    if (!circle) return;
    e.preventDefault();
    const node = state.byId.get(circle.dataset.node);
    if (!node) return;
    state.drafting = { node: node.id, port: circle.dataset.port, dir: circle.dataset.dir };
    $("#draft").style.display = "block";
    const start = portPos(node, circle.dataset.port, circle.dataset.dir);
    const world = screenToWorld(e.clientX, e.clientY);
    if (start) $("#draft-path").setAttribute("d", bezier(start, world));
  });

  document.addEventListener("mousemove", (e) => {
    if (!state.drafting) return;
    const node = state.byId.get(state.drafting.node);
    if (!node) return;
    const start = portPos(node, state.drafting.port, state.drafting.dir);
    if (!start) return;
    $("#draft-path").setAttribute("d",
      bezier(start, screenToWorld(e.clientX, e.clientY)));
  });

  document.addEventListener("mouseup", (e) => {
    if (!state.drafting) return;
    $("#draft").style.display = "none";
    const started = state.drafting;
    state.drafting = null;

    const under = document.elementFromPoint(e.clientX, e.clientY);
    const circle = under && under.closest && under.closest("circle.port-circle");
    if (!circle) return;

    const other = { node: circle.dataset.node, port: circle.dataset.port, dir: circle.dataset.dir };
    if (started.dir === "out" && other.dir === "in") addEdge(started.node, started.port, other.node, other.port);
    else if (started.dir === "in" && other.dir === "out") addEdge(other.node, other.port, started.node, started.port);
  });
}

// ==========================================================================
// Панорама, зум, клики по фону и рёбрам
// ==========================================================================
function setupCanvasEvents() {
  svg.addEventListener("mousedown", (e) => {
    if (!spaceDown && e.target.closest
        && (e.target.closest(".node-group") || e.target.closest("circle.port-circle"))) return;
    if (e.button !== 0 && e.button !== 1) return;
    e.preventDefault();
    const edge = e.target.closest && e.target.closest(".edge-hit");
    state.panning = {
      sx: e.clientX, sy: e.clientY,
      vx: state.view.x, vy: state.view.y,
      moved: false, edge: spaceDown ? null : edge,
    };
    svg.classList.add("panning");
    if (e.button === 0 && !edge && !spaceDown) clearSelection();
  });

  document.addEventListener("mousemove", (e) => {
    const pan = state.panning;
    if (!pan) return;
    const dx = e.clientX - pan.sx, dy = e.clientY - pan.sy;
    if (Math.abs(dx) + Math.abs(dy) > 3) pan.moved = true;
    state.view.x = pan.vx + dx;
    state.view.y = pan.vy + dy;
    applyView();
  });

  document.addEventListener("mouseup", () => {
    const pan = state.panning;
    state.panning = null;
    svg.classList.remove("panning");
    if (pan && !pan.moved && pan.edge) openEdgeMenu(pan.edge.dataset.edge);
  });

  document.addEventListener("mousedown", (e) => {
    if (!state.edgeMenu) return;
    if (e.target.closest && e.target.closest("#edge-menu")) return;
    closeEdgeMenu();
  }, true);

  svg.addEventListener("wheel", (e) => {
    e.preventDefault();
    const rect = svg.getBoundingClientRect();
    const factor = e.deltaY < 0 ? 1.12 : 1 / 1.12;
    const world = screenToWorld(e.clientX, e.clientY);
    state.zoom = Math.min(3, Math.max(0.25, state.zoom * factor));
    state.view.x = e.clientX - rect.left - world.x * state.zoom;
    state.view.y = e.clientY - rect.top - world.y * state.zoom;
    applyView();
    updateHudZoom();
  }, { passive: false });

  svg.addEventListener("dragover", (e) => e.preventDefault());
  svg.addEventListener("drop", (e) => {
    e.preventDefault();
    const type = e.dataTransfer.getData(MIME);
    if (!type) return;
    const point = screenToWorld(e.clientX, e.clientY);
    addNode(type, point.x - NODE_W / 2, point.y - 20);
  });

  document.addEventListener("keydown", (e) => {
    if (e.code === "Space" && !isTyping(e.target)) {
      spaceDown = true;
      svg.classList.add("panning");
      e.preventDefault();
    }
  });
  document.addEventListener("keyup", (e) => {
    if (e.code === "Space") {
      spaceDown = false;
      svg.classList.remove("panning");
    }
  });
}

// ==========================================================================
// Перемещение нод
// ==========================================================================
function setupNodeDrag() {
  let drag = null;

  document.addEventListener("mousedown", (e) => {
    if (e.button !== 0) return;
    const group = e.target.closest && e.target.closest(".node-group");
    if (!group) return;
    if (e.target.closest("circle.port-circle")) return;
    e.preventDefault();

    const id = group.dataset.id;
    if (e.shiftKey) {
      toggleSelection(id);
      return;
    }
    if (!state.selection.has(id)) selectOnly(id);
    else { state.active = id; updateSelectionDOM(); renderInspector(); }

    const start = screenToWorld(e.clientX, e.clientY);
    const items = [...state.selection]
      .map((nodeId) => state.byId.get(nodeId))
      .filter(Boolean)
      .map((node) => ({ node, x: node.x, y: node.y }));
    drag = { start, items, moved: false, before: snapshot() };
  });

  document.addEventListener("mousemove", (e) => {
    if (!drag) return;
    const world = screenToWorld(e.clientX, e.clientY);
    const dx = world.x - drag.start.x, dy = world.y - drag.start.y;
    if (Math.abs(dx) + Math.abs(dy) > 2) drag.moved = true;
    if (!drag.moved) return;
    drag.items.forEach((item) => {
      item.node.x = Math.round(item.x + dx);
      item.node.y = Math.round(item.y + dy);
      const group = nodeGroup(item.node.id);
      if (group) group.setAttribute("transform", `translate(${item.node.x},${item.node.y})`);
    });
    renderEdges();
  });

  document.addEventListener("mouseup", () => {
    if (!drag) return;
    if (drag.moved) {
      recordSnapshot(drag.before);
      markDirty();
    }
    drag = null;
  });
}

// ==========================================================================
// Выделение и инспектор
// ==========================================================================
function isTyping(target) {
  if (!target) return false;
  const tag = target.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || target.isContentEditable;
}

function updateSelectionDOM() {
  document.querySelectorAll(".node-group").forEach((group) => {
    group.classList.toggle("selected", state.selection.has(group.dataset.id));
  });
}

function selectOnly(id) {
  state.selection = new Set([id]);
  state.active = id;
  updateSelectionDOM();
  renderInspector();
}

function toggleSelection(id) {
  if (state.selection.has(id)) state.selection.delete(id);
  else state.selection.add(id);
  state.active = state.selection.has(id) ? id : [...state.selection].pop() || null;
  updateSelectionDOM();
  renderInspector();
}

function clearSelection() {
  if (!state.selection.size && !state.active) return;
  state.selection.clear();
  state.active = null;
  updateSelectionDOM();
  renderInspector();
}

function renderInspector() {
  const node = state.active ? state.byId.get(state.active) : null;
  if (!node) {
    $("#insp-empty").style.display = "block";
    $("#insp-form").style.display = "none";
    return;
  }
  $("#insp-empty").style.display = "none";
  $("#insp-form").style.display = "block";
  $("#insp-title").textContent = node.title;
  $("#insp-type").textContent = (node.spec.category || "") + " · " + node.type +
    (state.selection.size > 1 ? ` · выбрано ${state.selection.size}` : "");

  const note = $("#insp-note");
  if (node.spec.note) {
    note.textContent = node.spec.note;
    note.style.display = "block";
  } else {
    note.style.display = "none";
  }

  const fields = $("#insp-fields");
  fields.innerHTML = "";

  (node.spec.params || []).forEach((param) => {
    const wrap = document.createElement("div");
    wrap.className = "field";
    const label = document.createElement("label");
    label.textContent = param.label;
    wrap.appendChild(label);

    let input;
    if (param.type === "select") {
      input = document.createElement("select");
      (param.options || []).forEach((option) => {
        const opt = document.createElement("option");
        opt.value = option;
        opt.textContent = option;
        input.appendChild(opt);
      });
      input.value = node.params[param.name];
      input.addEventListener("change", () => {
        scheduleHistory();
        node.params[param.name] = input.value;
        markDirty();
      });
    } else if (param.type === "bool") {
      input = document.createElement("input");
      input.type = "checkbox";
      input.checked = !!node.params[param.name];
      input.addEventListener("change", () => {
        scheduleHistory();
        node.params[param.name] = input.checked;
        markDirty();
      });
    } else if (param.type === "int") {
      input = document.createElement("input");
      input.type = "number";
      input.value = node.params[param.name] ?? "";
      input.addEventListener("input", () => {
        scheduleHistory();
        if (input.value === "") {
          input.classList.remove("invalid");
          node.params[param.name] = "";
        } else if (Number.isFinite(Number(input.value))) {
          input.classList.remove("invalid");
          node.params[param.name] = Number(input.value);
        } else {
          input.classList.add("invalid");
        }
        markDirty();
      });
    } else if (param.type === "color") {
      input = document.createElement("input");
      input.type = "color";
      input.value = node.params[param.name] || "#3b82f6";
      input.addEventListener("input", () => {
        scheduleHistory();
        node.params[param.name] = input.value;
        const group = nodeGroup(node.id);
        if (group) group.style.setProperty("--nodecolor", input.value);
        markDirty();
      });
    } else if (param.type === "textarea") {
      input = document.createElement("textarea");
      input.value = node.params[param.name] ?? "";
      input.addEventListener("input", () => {
        scheduleHistory();
        node.params[param.name] = input.value;
        markDirty();
      });
    } else {
      input = document.createElement("input");
      input.type = "text";
      input.value = node.params[param.name] ?? "";
      input.addEventListener("input", () => {
        scheduleHistory();
        node.params[param.name] = input.value;
        markDirty();
      });
    }

    wrap.appendChild(input);
    if (param.hint) {
      const hint = document.createElement("div");
      hint.className = "hint";
      hint.textContent = param.hint;
      wrap.appendChild(hint);
    }
    fields.appendChild(wrap);
  });

  $("#insp-delete").onclick = () => deleteSelected();
}

// ==========================================================================
// Удаление
// ==========================================================================
function deleteSelected() {
  if (!state.selection.size) return;
  closeEdgeMenu();
  pushHistory();
  const ids = new Set(state.selection);
  state.graph.nodes = state.graph.nodes.filter((n) => !ids.has(n.id));
  state.graph.edges = state.graph.edges.filter(
    (e) => !ids.has(e.source) && !ids.has(e.target));
  reindex();
  state.selection.clear();
  state.active = null;
  renderAll();
  renderInspector();
  markDirty();
  setStatus(`Удалено нод: ${ids.size}`);
}

// ==========================================================================
// История (отмена / повтор)
// ==========================================================================
function snapshot() {
  return JSON.stringify({
    nodes: state.graph.nodes.map((n) => ({
      id: n.id, type: n.type, x: n.x, y: n.y, params: n.params,
    })),
    edges: state.graph.edges,
  });
}

function recordSnapshot(snap) {
  if (!snap) return;
  if (state.undo.length && state.undo[state.undo.length - 1] === snap) return;
  state.undo.push(snap);
  if (state.undo.length > 60) state.undo.shift();
  state.redo.length = 0;
  updateHistoryButtons();
}

function pushHistory() {
  recordSnapshot(snapshot());
}

function scheduleHistory() {
  if (state.pendingHistory === null) state.pendingHistory = snapshot();
  clearTimeout(state.historyTimer);
  state.historyTimer = setTimeout(() => {
    recordSnapshot(state.pendingHistory);
    state.pendingHistory = null;
  }, 500);
}

function restoreSnapshot(snap) {
  closeEdgeMenu();
  const data = JSON.parse(snap);
  state.graph = {
    nodes: data.nodes.map((n) => hydrateNode({ ...n })),
    edges: data.edges.map((e) => ({ ...e })),
  };
  reindex();
  renderAll();
  renderInspector();
}

function undo() {
  if (!state.undo.length) return;
  const current = snapshot();
  const previous = state.undo.pop();
  state.redo.push(current);
  restoreSnapshot(previous);
  markDirty();
  updateHistoryButtons();
}

function redo() {
  if (!state.redo.length) return;
  const current = snapshot();
  const next = state.redo.pop();
  state.undo.push(current);
  restoreSnapshot(next);
  markDirty();
  updateHistoryButtons();
}

function updateHistoryButtons() {
  $("#btn-undo").disabled = !state.undo.length;
  $("#btn-redo").disabled = !state.redo.length;
}

// ==========================================================================
// Автораскладка
// ==========================================================================
function autoLayout() {
  if (!state.graph.nodes.length) return;
  pushHistory();

  const nodes = state.graph.nodes;
  const indegree = new Map(nodes.map((n) => [n.id, 0]));
  const children = new Map(nodes.map((n) => [n.id, []]));
  state.graph.edges.forEach((edge) => {
    if (!children.has(edge.source) || !indegree.has(edge.target)) return;
    indegree.set(edge.target, indegree.get(edge.target) + 1);
    children.get(edge.source).push(edge.target);
  });

  const depth = new Map();
  const pending = new Map(indegree);
  const queue = nodes.filter((n) => indegree.get(n.id) === 0).map((n) => n.id);
  queue.forEach((id) => depth.set(id, 0));

  let cursor = 0;
  while (cursor < queue.length) {
    const id = queue[cursor++];
    for (const child of children.get(id)) {
      depth.set(child, Math.max(depth.get(child) ?? 0, (depth.get(id) ?? 0) + 1));
      pending.set(child, pending.get(child) - 1);
      if (pending.get(child) === 0) queue.push(child);
    }
  }

  const maxDepth = Math.max(0, ...depth.values());
  nodes.forEach((node) => {
    if (!depth.has(node.id)) depth.set(node.id, maxDepth + 1);
  });

  const layers = new Map();
  nodes.forEach((node) => {
    const level = depth.get(node.id);
    if (!layers.has(level)) layers.set(level, []);
    layers.get(level).push(node);
  });

  [...layers.keys()].sort((a, b) => a - b).forEach((level) => {
    const column = layers.get(level);
    column.sort((a, b) => (a.y - b.y) || a.title.localeCompare(b.title));
    column.forEach((node, index) => {
      node.x = level * 270;
      node.y = index * 140;
    });
  });

  renderAll();
  fitView();
  markDirty();
}

// ==========================================================================
// Прогон
// ==========================================================================
function setBusy(busy) {
  state.busy = busy;
  const button = $("#btn-run");
  button.disabled = busy;
  button.innerHTML = busy
    ? '<i class="fas fa-circle-notch fa-spin"></i> Выполняется'
    : '<i class="fas fa-play"></i> Запустить';
  const cancel = $("#btn-cancel");
  cancel.style.display = busy ? "" : "none";
  cancel.disabled = !busy;
}

function graphPayload() {
  return JSON.stringify({
    nodes: state.graph.nodes.map((n) => ({
      id: n.id, type: n.type, x: n.x, y: n.y, params: n.params,
    })),
    edges: state.graph.edges,
    seed: {},
  });
}

async function runGraph() {
  if (state.busy) return;
  if (!state.graph.nodes.length) { setStatus("Граф пуст: добавьте ноды", true); return; }
  if (state.settings && state.settings.run && state.settings.run.autosave_before_run) {
    await saveGraph(true);
  }
  setBusy(true);
  state.progress = {};
  setStatus("Запуск…");
  try {
    const res = await fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: graphPayload(),
    });
    const data = await res.json().catch(() => null);
    if (!res.ok || !data || data.ok === false) {
      setBusy(false);
      setStatus("Не удалось запустить: " +
        ((data && data.error) || res.status), true);
      return;
    }
    state.runId = data.run_id;
    startProgressPolling();
  } catch (err) {
    setBusy(false);
    setStatus("Ошибка сети: " + err.message, true);
  }
}

function startProgressPolling() {
  stopProgressPolling();
  state.runTimer = setInterval(pollRun, 350);
  pollRun();
}

function stopProgressPolling() {
  if (state.runTimer) clearInterval(state.runTimer);
  state.runTimer = null;
}

function progressCounts(progress) {
  const counts = { done: 0, skipped: 0, error: 0, cycle: 0,
    total: 0, finished: 0 };
  Object.values(progress || {}).forEach((entry) => {
    counts.total += 1;
    if (entry.status && entry.status !== "running") counts.finished += 1;
    if (counts[entry.status] !== undefined) counts[entry.status] += 1;
  });
  return counts;
}

function renderProgress(progress) {
  const counts = progressCounts(progress);
  state.report = progress || {};
  applyStatuses();
  const percent = counts.total
    ? Math.round((counts.finished / counts.total) * 100) : 0;
  $("#progress-bar").style.display = "";
  $("#progress-fill").style.width = percent + "%";
  setStatus(`Выполняется: ${counts.finished}/${counts.total} · ` +
    `${counts.done} готово, ${counts.error} ошибок`);
  return percent;
}

async function pollRun() {
  const id = state.runId;
  if (!id) return;
  let data = null;
  try {
    const res = await fetch("/api/run/" + encodeURIComponent(id));
    data = await res.json().catch(() => null);
    if (!res.ok || !data) {
      throw new Error((data && data.error) || res.status);
    }
  } catch (err) {
    stopProgressPolling();
    state.runId = null;
    setBusy(false);
    setStatus("Проверка прогона не удалась: " + err.message, true);
    return;
  }
  renderProgress(data.progress);
  if (data.status === "running") return;

  stopProgressPolling();
  state.runId = null;
  setBusy(false);
  const result = data.result || {};
  state.report = result.report || {};
  applyStatuses();
  showResult({
    report: result.report || {},
    result: result.result || {},
    order: result.order || [],
    summary: data.summary || result.summary || {},
  });
  const labels = {
    done: "Готово", error: "Завершено с ошибками",
    cancelled: "Остановлено", timeout: "Превышен таймаут",
  };
  const s = data.summary || {};
  setStatus(`${labels[data.status] || data.status}: ${s.done || 0} выполнено, ` +
    `${s.skipped || 0} пропущено, ${s.error || 0} ошибок`,
    data.status === "error" || data.status === "timeout");
  $("#progress-fill").style.width = "100%";
  setTimeout(() => {
    $("#progress-bar").style.display = "none";
    $("#progress-fill").style.width = "0";
  }, 1500);
}

async function cancelRun() {
  if (!state.runId) { setStatus("Нет активного прогона", true); return; }
  try {
    const res = await fetch("/api/cancel", { method: "POST" });
    const data = await res.json().catch(() => null);
    if (!res.ok || !data || !data.ok) {
      setStatus("Не удалось остановить: " +
        ((data && data.error) || res.status), true);
      return;
    }
    setStatus("Останавливаю прогон…");
  } catch (err) {
    setStatus("Ошибка сети: " + err.message, true);
  }
}

function applyStatuses() {
  document.querySelectorAll(".node-group").forEach((group) => {
    group.classList.remove("status-done", "status-error", "status-skipped",
      "status-cycle", "status-running");
    const info = state.report && state.report[group.dataset.id];
    if (info && info.status) group.classList.add("status-" + info.status);
  });
}

function clearStatuses() {
  state.report = null;
  document.querySelectorAll(".node-group").forEach((group) => {
    group.classList.remove("status-done", "status-error", "status-skipped",
      "status-cycle", "status-running");
  });
}

const STATUS_LABEL = {
  done: "выполнено", skipped: "пропущено", error: "ошибка", cycle: "цикл",
};

function showResult(data) {
  const panel = $("#result-panel");
  panel.style.display = "flex";
  const summary = data.summary || {};
  $("#result-title").textContent =
    `Результат: ${summary.done || 0} выполнено, ${summary.skipped || 0} пропущено, ` +
    `${summary.error || 0} ошибок`;

  const body = $("#result-body");
  body.innerHTML = "";

  const order = (data.order || []).concat(
    Object.keys(data.report || {}).filter((id) => !(data.order || []).includes(id)));

  order.forEach((id) => {
    const node = state.byId.get(id);
    const info = data.report[id] || {};
    const row = document.createElement("div");
    row.className = "res-node";

    const head = document.createElement("div");
    head.className = "res-head";
    const name = document.createElement("span");
    name.className = "res-name";
    name.textContent = node ? node.title : id;
    head.appendChild(name);

    const status = document.createElement("span");
    status.className = "res-status " + (info.status || "skipped");
    status.textContent = STATUS_LABEL[info.status] || info.status || "—";
    head.appendChild(status);

    if (info.ms !== undefined) {
      const ms = document.createElement("span");
      ms.className = "res-ms";
      ms.textContent = info.ms + " мс";
      head.appendChild(ms);
    }
    row.appendChild(head);

    if (info.error || info.reason || info.warning) {
      const message = document.createElement("div");
      message.className = "res-msg " + (info.status || "");
      message.textContent = info.error || info.reason || info.warning;
      row.appendChild(message);
    }

    const outputs = data.result && data.result[id];
    if (outputs && Object.keys(outputs).length) {
      const pre = document.createElement("div");
      pre.className = "res-json";
      pre.textContent = JSON.stringify(outputs, null, 1);
      row.appendChild(pre);
    }
    body.appendChild(row);
  });
}

$("#result-close").onclick = () => { $("#result-panel").style.display = "none"; };

// ==========================================================================
// Настройки, тема и автосохранение
// ==========================================================================
const SETTINGS_SECTIONS = [
  {
    key: "run", title: "Прогон",
    fields: [
      { key: "pause_ms", label: "Пауза между нодами, мс", type: "number", min: 0, max: 5000 },
      { key: "timeout_s", label: "Таймаут прогона, с", type: "number", min: 5, max: 3600 },
      { key: "on_error", label: "При ошибке", type: "select",
        options: [["continue", "продолжать"], ["stop", "остановить прогон"]] },
      { key: "retries", label: "Повторов при ошибке", type: "number", min: 0, max: 3 },
      { key: "autosave_before_run", label: "Сохранять граф перед запуском",
        type: "checkbox", full: true },
    ],
  },
  {
    key: "files", title: "Файлы",
    fields: [
      { key: "autosave_sec", label: "Автосохранение, с (0 — выключено)",
        type: "number", min: 0, max: 600 },
      { key: "backups", label: "Резервных копий графа", type: "number", min: 0, max: 50 },
      { key: "workspace", label: "Рабочая папка для файлов", type: "text", full: true,
        hint: "Абсолютный путь. Пусто — папка по умолчанию рядом с графом." },
    ],
  },
  {
    key: "mail", title: "Почта",
    fields: [
      { key: "smtp_host", label: "SMTP-сервер", type: "text" },
      { key: "smtp_port", label: "Порт", type: "number", min: 1, max: 65535 },
      { key: "smtp_user", label: "Пользователь", type: "text" },
      { key: "from_addr", label: "Отправитель по умолчанию", type: "text" },
      { key: "password_env", label: "Переменная с паролем", type: "text" },
      { key: "tls", label: "STARTTLS", type: "checkbox" },
    ],
  },
  {
    key: "ui", title: "Интерфейс",
    fields: [
      { key: "theme", label: "Тема", type: "select",
        options: [["dark", "тёмная"], ["light", "светлая"]] },
      { key: "grid", label: "Шаг сетки, px", type: "number", min: 8, max: 64 },
    ],
  },
  {
    key: "webhook", title: "Вебхук",
    fields: [
      { key: "enabled", label: "Принимать входящие вебхуки", type: "checkbox" },
      { key: "token_env", label: "Переменная с токеном", type: "text" },
    ],
  },
  {
    key: "security", title: "Безопасность",
    fields: [
      { key: "allow_private_urls", label: "Разрешить запросы во внутреннюю сеть",
        type: "checkbox", full: true,
        hint: "По умолчанию запрещено: защита от SSRF на localhost и в локальной сети." },
    ],
  },
];

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme === "light" ? "light" : "dark";
}

function applyUiSettings() {
  const ui = (state.settings && state.settings.ui) || {};
  applyTheme(ui.theme);
  const grid = Number(ui.grid) || 24;
  const pattern = $("#grid");
  if (pattern) {
    pattern.setAttribute("width", grid);
    pattern.setAttribute("height", grid);
    const line = pattern.querySelector("path");
    if (line) line.setAttribute("d", `M ${grid} 0 L 0 0 0 ${grid}`);
  }
}

function settingsValue(section, key) {
  const part = state.settings && state.settings[section];
  return part ? part[key] : undefined;
}

function fieldValue(section, key) {
  const value = settingsValue(section, key);
  return value === undefined || value === null ? "" : value;
}

async function loadSettings() {
  try {
    const res = await fetch("/api/settings");
    const data = await res.json();
    if (data && data.settings) {
      state.settings = data.settings;
      applyUiSettings();
      setupAutosave();
    }
  } catch (err) {
    console.warn("Настройки недоступны:", err.message);
  }
}

function renderSettingsTabs() {
  const box = $("#settings-tabs");
  box.innerHTML = "";
  SETTINGS_SECTIONS.forEach((section) => {
    const tab = document.createElement("button");
    tab.className = "tab" + (state.settingsTab === section.key ? " active" : "");
    tab.textContent = section.title;
    tab.onclick = () => {
      state.settingsTab = section.key;
      renderSettingsTabs();
      renderSettingsForm();
    };
    box.appendChild(tab);
  });
}

function renderSettingsForm() {
  const section = SETTINGS_SECTIONS.find((item) => item.key === state.settingsTab)
    || SETTINGS_SECTIONS[0];
  const box = $("#settings-form");
  box.innerHTML = "";
  const grid = document.createElement("div");
  grid.className = "settings-grid";

  section.fields.forEach((field) => {
    const wrap = document.createElement("div");
    wrap.className = "field" + (field.full ? " full" : "");
    const label = document.createElement("label");
    label.textContent = field.label;
    wrap.appendChild(label);

    let input;
    if (field.type === "select") {
      input = document.createElement("select");
      field.options.forEach(([value, text]) => {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = text;
        input.appendChild(option);
      });
      input.value = String(fieldValue(section.key, field.key));
      if (!input.value && field.options.length) input.value = field.options[0][0];
    } else if (field.type === "checkbox") {
      input = document.createElement("input");
      input.type = "checkbox";
      input.checked = !!settingsValue(section.key, field.key);
    } else {
      input = document.createElement("input");
      input.type = field.type === "number" ? "number" : "text";
      if (field.min !== undefined) input.min = field.min;
      if (field.max !== undefined) input.max = field.max;
      input.value = fieldValue(section.key, field.key);
    }
    input.dataset.section = section.key;
    input.dataset.key = field.key;
    input.dataset.kind = field.type;
    wrap.appendChild(input);

    if (field.hint) {
      const hint = document.createElement("div");
      hint.className = "hint";
      hint.textContent = field.hint;
      wrap.appendChild(hint);
    }
    grid.appendChild(wrap);
  });

  box.appendChild(grid);
  renderSettingsEnv(section.key);
}

function renderSettingsEnv(sectionKey) {
  const box = $("#settings-env");
  const env = (state.settings && state.settings.env) || {};
  const lines = [];
  if (sectionKey === "mail") {
    lines.push(`Пароль SMTP: ${env.smtp_password_set ? "задан" : "не задан"} ` +
      `(${env.smtp_password_env || "FLOWZA_SMTP_PASSWORD"})`);
  } else if (sectionKey === "webhook") {
    lines.push(`Токен: ${env.webhook_token_set ? "задан" : "не задан"} ` +
      `(${env.webhook_token_env || "FLOWZA_WEBHOOK_TOKEN"})`);
    lines.push("Приём: POST /hook/<суффикс>. Без токена принимаются только локальные запросы.");
  } else if (sectionKey === "files") {
    lines.push("Рабочая папка: " +
      ((state.settings && state.settings.files
        && state.settings.files.workspace_resolved) || "—"));
  } else if (sectionKey === "security") {
    lines.push("Внутренние адреса запрещены по умолчанию — это защита от SSRF.");
  }
  box.textContent = lines.join(" · ");
  box.style.display = lines.length ? "" : "none";
}

function isModalOpen() {
  return ["#settings-modal", "#history-modal"].some((selector) => {
    const el = $(selector);
    return el && el.style.display !== "none";
  });
}

function closeModals() {
  ["#settings-modal", "#history-modal", "#modal-backdrop"].forEach((selector) => {
    const el = $(selector);
    if (el) el.style.display = "none";
  });
}

async function openSettings() {
  $("#modal-backdrop").style.display = "";
  $("#settings-modal").style.display = "flex";
  try {
    const res = await fetch("/api/settings");
    const data = await res.json();
    if (data && data.settings) state.settings = data.settings;
  } catch (err) {
    console.warn("Настройки недоступны:", err.message);
  }
  $("#settings-paths").textContent =
    "Граф: " + ((state.settings && state.settings.graph_file) || "—");
  $("#settings-warnings").textContent = "";
  renderSettingsTabs();
  renderSettingsForm();
}

function settingInputs() {
  return [...document.querySelectorAll("#settings-form [data-section]")];
}

async function saveSettings() {
  const patch = {};
  settingInputs().forEach((input) => {
    const section = input.dataset.section;
    const key = input.dataset.key;
    const kind = input.dataset.kind;
    let value;
    if (kind === "checkbox") value = input.checked;
    else if (kind === "number") value = input.value === "" ? 0 : Number(input.value);
    else value = input.value;
    (patch[section] ||= {})[key] = value;
  });
  try {
    const res = await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    });
    const data = await res.json().catch(() => null);
    if (!res.ok || !data || !data.ok) {
      setStatus("Настройки не сохранены: " +
        ((data && data.error) || res.status), true);
      return;
    }
    state.settings = data.settings;
    applyUiSettings();
    setupAutosave();
    const warnings = data.warnings || [];
    $("#settings-warnings").textContent = warnings.join("; ");
    setStatus("Настройки сохранены");
    if (!warnings.length) closeModals();
  } catch (err) {
    setStatus("Ошибка сети: " + err.message, true);
  }
}

function setupAutosave() {
  if (state.autosaveTimer) clearInterval(state.autosaveTimer);
  state.autosaveTimer = null;
  const seconds = Number((state.settings && state.settings.files
    && state.settings.files.autosave_sec) || 0);
  if (seconds > 0) {
    state.autosaveTimer = setInterval(() => {
      if (state.dirty && !state.busy) saveGraph(true);
    }, seconds * 1000);
  }
}

// ==========================================================================
// История прогонов
// ==========================================================================
function runStatusLabel(status) {
  return {
    done: "готово", error: "ошибка", cancelled: "отменено",
    timeout: "таймаут", running: "идёт",
  }[status] || status;
}

async function openHistory() {
  $("#modal-backdrop").style.display = "";
  $("#history-modal").style.display = "flex";
  await refreshHistory();
}

async function refreshHistory() {
  const box = $("#history-body");
  box.innerHTML = '<div class="history-empty">Загрузка…</div>';
  let runs = [];
  try {
    const res = await fetch("/api/runs");
    const data = await res.json().catch(() => null);
    runs = (data && data.runs) || [];
  } catch (err) {
    box.innerHTML = '<div class="history-empty">Не удалось загрузить историю</div>';
    return;
  }
  box.innerHTML = "";
  if (!runs.length) {
    box.innerHTML = '<div class="history-empty">Пока нет ни одного прогона</div>';
    return;
  }
  runs.forEach((run) => {
    const row = document.createElement("div");
    row.className = "history-row";

    const when = document.createElement("span");
    when.className = "history-when";
    when.textContent = run.started_wall
      ? new Date(run.started_wall * 1000).toLocaleString("ru-RU",
        { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })
      : "—";
    row.appendChild(when);

    const source = document.createElement("span");
    source.className = "history-source";
    source.textContent = { manual: "вручную", schedule: "расписание",
      webhook: "вебхук" }[run.source] || run.source;
    row.appendChild(source);

    const summary = document.createElement("span");
    summary.className = "history-summary";
    const s = run.summary || {};
    summary.textContent = `${s.done || 0} / ${s.skipped || 0} / ${s.error || 0}`;
    row.appendChild(summary);

    const badge = document.createElement("span");
    badge.className = "run-badge " + (run.status || "done");
    badge.textContent = runStatusLabel(run.status);
    row.appendChild(badge);

    const open = document.createElement("button");
    open.className = "history-open";
    open.textContent = "Открыть";
    open.onclick = () => openRun(run.run_id);
    row.appendChild(open);

    box.appendChild(row);
  });
}

async function openRun(runId) {
  try {
    const res = await fetch("/api/run/" + encodeURIComponent(runId));
    const data = await res.json().catch(() => null);
    if (!res.ok || !data || !data.result) {
      setStatus("Результат этого прогона недоступен", true);
      return;
    }
    closeModals();
    const result = data.result;
    state.report = result.report || {};
    applyStatuses();
    showResult({
      report: result.report || {},
      result: result.result || {},
      order: result.order || [],
      summary: data.summary || result.summary || {},
    });
  } catch (err) {
    setStatus("Ошибка сети: " + err.message, true);
  }
}

// ==========================================================================
// Сохранение / загрузка
// ==========================================================================
function payloadOfGraph() {
  return JSON.stringify({
    nodes: state.graph.nodes.map((n) => ({
      id: n.id, type: n.type, x: n.x, y: n.y, params: n.params,
    })),
    edges: state.graph.edges,
  });
}

async function saveGraph(silent = false) {
  try {
    const res = await fetch("/api/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: payloadOfGraph(),
    });
    const data = await res.json().catch(() => null);
    if (!res.ok || !data || !data.ok) {
      setStatus("Не удалось сохранить: " + ((data && data.error) || res.status), true);
      return;
    }
    state.dirty = false;
    document.title = "Flowza — конструктор автоматизаций";
    if (!silent) setStatus("Сохранено → " + data.file);
  } catch (err) {
    setStatus("Ошибка сети: " + err.message, true);
  }
}

async function loadGraph(reload = false) {
  if (state.dirty && !confirm("Несохранённые изменения будут потеряны. Продолжить?")) return;
  try {
    const res = await fetch(reload ? "/api/reload" : "/api/graph", {
      method: reload ? "POST" : "GET",
    });
    const data = await res.json().catch(() => null);
    if (!res.ok || !data) { setStatus("Не удалось прочитать граф", true); return; }
    state.graph = {
      nodes: (data.nodes || []).map((n) => hydrateNode({
        id: n.id, type: n.type, x: n.x, y: n.y, params: n.params,
      })),
      edges: (data.edges || []).map((e) => ({ ...e })),
    };
    state.selection.clear();
    state.active = null;
    state.undo = [];
    state.redo = [];
    state.dirty = false;
    closeEdgeMenu();
    document.title = "Flowza — конструктор автоматизаций";
    reindex();
    clearStatuses();
    renderAll();
    renderInspector();
    updateHistoryButtons();
    fitView();
    if (data.warnings && data.warnings.length) {
      setStatus("Загружено с замечаниями: " + data.warnings.join("; "), true);
    } else {
      setStatus("Граф загружен");
    }
  } catch (err) {
    setStatus("Ошибка сети: " + err.message, true);
  }
}

function newGraph() {
  if (state.dirty && !confirm("Очистить граф? Несохранённое будет потеряно.")) return;
  closeEdgeMenu();
  pushHistory();
  state.graph = { nodes: [], edges: [] };
  state.selection.clear();
  state.active = null;
  state.byId.clear();
  clearStatuses();
  renderAll();
  renderInspector();
  state.dirty = false;
  document.title = "Flowza — конструктор автоматизаций";
  markDirty();
  setStatus("Новый граф");
}

// ==========================================================================
// Служебное
// ==========================================================================
function markDirty() {
  if (state.report) clearStatuses();
  if (!state.dirty) {
    state.dirty = true;
    document.title = "* Flowza — конструктор автоматизаций";
  }
}

function setStatus(text, isError = false) {
  const line = $("#tb-status");
  line.textContent = text;
  line.style.color = isError ? "var(--danger)" : "var(--muted)";
}

function updateHudZoom() {
  $("#hud-zoom").textContent = Math.round(state.zoom * 100) + "%";
}

function fitView() {
  if (!state.graph.nodes.length) {
    state.view = { x: 40, y: 40 };
    state.zoom = 1;
    applyView();
    updateHudZoom();
    return;
  }
  const xs = state.graph.nodes.map((n) => n.x);
  const ys = state.graph.nodes.map((n) => n.y);
  const minX = Math.min(...xs), maxX = Math.max(...xs) + NODE_W;
  const minY = Math.min(...ys);
  const maxY = Math.max(...state.graph.nodes.map((n) => n.y + nodeHeight(n)));
  const rect = svg.getBoundingClientRect();
  const z = Math.min(1.2, Math.max(0.25,
    Math.min((rect.width - 40) / (maxX - minX + 40),
             (rect.height - 40) / (maxY - minY + 40))));
  state.zoom = z;
  state.view.x = rect.width / 2 - (maxX + minX) / 2 * z;
  state.view.y = rect.height / 2 - (maxY + minY) / 2 * z;
  applyView();
  updateHudZoom();
}

function zoomDelta(factor) {
  const rect = svg.getBoundingClientRect();
  const world = screenToWorld(rect.left + rect.width / 2, rect.top + rect.height / 2);
  state.zoom = Math.min(3, Math.max(0.25, state.zoom * factor));
  state.view.x = rect.width / 2 - world.x * state.zoom;
  state.view.y = rect.height / 2 - world.y * state.zoom;
  applyView();
  updateHudZoom();
}

// ==========================================================================
// Инициализация
// ==========================================================================
function bindToolbar() {
  $("#btn-new").onclick = newGraph;
  $("#btn-open").onclick = () => loadGraph(true);
  $("#btn-save").onclick = () => saveGraph();
  $("#btn-run").onclick = runGraph;
  $("#btn-cancel").onclick = cancelRun;
  $("#btn-autolayout").onclick = autoLayout;
  $("#btn-delete").onclick = deleteSelected;
  $("#btn-undo").onclick = undo;
  $("#btn-redo").onclick = redo;
  $("#btn-settings").onclick = openSettings;
  $("#btn-history").onclick = openHistory;
  $("#settings-save").onclick = saveSettings;
  $("#history-refresh").onclick = refreshHistory;
  $("#modal-backdrop").onclick = closeModals;
  document.querySelectorAll("[data-close]").forEach((button) => {
    button.onclick = closeModals;
  });
  $("#hud-in").onclick = () => zoomDelta(1.12);
  $("#hud-out").onclick = () => zoomDelta(1 / 1.12);
  $("#hud-fit").onclick = fitView;
  $("#palette-input").addEventListener("input", applyPaletteFilter);
  $("#edge-menu-delete").onclick = () => {
    const id = state.edgeMenu && state.edgeMenu.id;
    if (id) removeEdge(id);
  };
}

document.addEventListener("keydown", (e) => {
  const typing = isTyping(e.target);
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
    e.preventDefault();
    saveGraph();
    return;
  }
  if (typing) return;

  if (e.key === "F5") { e.preventDefault(); runGraph(); return; }
  if ((e.key === "Delete" || e.key === "Backspace") && state.edgeMenu) {
    e.preventDefault();
    removeEdge(state.edgeMenu.id);
    return;
  }
  if (e.key === "Delete" || e.key === "Backspace") {
    e.preventDefault();
    deleteSelected();
    return;
  }
  if (e.key === "Escape") {
    if (isModalOpen()) { closeModals(); return; }
    closeEdgeMenu();
    clearSelection();
    return;
  }

  if (e.ctrlKey || e.metaKey) {
    const key = e.key.toLowerCase();
    if (key === "z" && !e.shiftKey) { e.preventDefault(); undo(); }
    else if (key === "z" || key === "y") { e.preventDefault(); redo(); }
    else if (key === "n") { e.preventDefault(); newGraph(); }
    else if (key === "o") { e.preventDefault(); loadGraph(true); }
    else if (key === "l") { e.preventDefault(); autoLayout(); }
  }
});

window.addEventListener("beforeunload", (e) => {
  if (!state.dirty) return;
  e.preventDefault();
  e.returnValue = "";
});

async function init() {
  try {
    await loadSpecs();
  } catch (err) {
    setStatus("Не удалось загрузить каталог нод: " + err.message, true);
    return;
  }
  await loadSettings();
  wirePortEvents();
  setupCanvasEvents();
  setupNodeDrag();
  bindToolbar();
  await loadGraph();
  updateHistoryButtons();
  setStatus("Готово");
}

init();
