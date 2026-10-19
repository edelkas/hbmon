"use strict";

// ---------------------------------------------------------------- utilities

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const api = {
  async get(url) {
    const r = await fetch(url);
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || r.statusText);
    return j;
  },
  async post(url, body = {}) {
    const r = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-HBMon": "1" },
      body: JSON.stringify(body),
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || r.statusText);
    return j;
  },
};

const store = {
  get(k, d) {
    try { const v = localStorage.getItem("hbmon." + k); return v === null ? d : JSON.parse(v); } catch { return d; }
  },
  set(k, v) { try { localStorage.setItem("hbmon." + k, JSON.stringify(v)); } catch { /* private mode */ } },
};

let toastTimer;
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), 5000);
}

function fmtBytes(n) {
  if (n == null || isNaN(n)) return "–";
  const u = ["B", "KiB", "MiB", "GiB", "TiB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i === 0 ? n.toFixed(0) : n.toFixed(n < 10 ? 2 : n < 100 ? 1 : 0)) + " " + u[i];
}
function fmtDur(s) {
  if (s == null || isNaN(s)) return "–";
  s = Math.max(0, Math.round(s));
  const d = Math.floor(s / 86400), h = Math.floor(s / 3600) % 24, m = Math.floor(s / 60) % 60, sec = s % 60;
  const hms = `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
  return d ? `${d}d ${hms}` : hms;
}
function fmtClock(ts) {
  if (!ts) return "–";
  const d = new Date(ts * 1000), now = new Date();
  const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  return d.toDateString() === now.toDateString() ? time : d.toLocaleDateString() + " " + time;
}
const fmtPct = (p) => (100 * (p || 0)).toFixed(1) + "%";
const bar = (p, run) => `<div class="bar"><i class="${run ? "run" : ""}" style="width:${(100 * Math.min(1, p || 0)).toFixed(2)}%"></i><b>${fmtPct(p)}</b></div>`;

// ---------------------------------------------------------------- app state

let config = { roots: [], sep: "/" };
let state = { jobs: [], summary: null, state: "idle" };
let jobsById = new Map();
const isWin = () => config.sep === "\\";

// ---------------------------------------------------------------- file tree

const tree = {
  roots: [],
  sel: new Set(),      // selected paths
  anchor: null,        // node for shift-range selection
  focus: null,         // focused node
  flat: [],            // visible nodes, in order
};

function mkNode(e, parent) {
  return { name: e.name, path: e.path, dir: e.dir, size: e.size, parent, depth: parent ? parent.depth + 1 : 0,
           children: null, expanded: false, loading: false };
}

function lsUrl(path) {
  return `/api/ls?path=${encodeURIComponent(path)}&all=${$("#tree-all").checked ? 1 : 0}&hidden=${$("#tree-hidden").checked ? 1 : 0}`;
}

async function loadChildren(node, keep) {
  node.loading = true;
  renderTree();
  try {
    const list = await api.get(lsUrl(node.path));
    const old = new Map((node.children || []).map((c) => [c.path, c]));
    node.children = list.map((e) => {
      const prev = old.get(e.path);
      if (prev && keep) { prev.size = e.size; return prev; }
      return mkNode(e, node);
    });
  } catch (err) {
    node.children = [];
    toast(`${node.path}: ${err.message}`);
  }
  node.loading = false;
}

async function toggleNode(node, open = !node.expanded) {
  if (!node.dir) return;
  node.expanded = open;
  if (open && node.children === null) await loadChildren(node);
  renderTree();
}

async function refreshTree() {
  const walk = async (nodes) => {
    for (const n of nodes) {
      if (n.dir && n.expanded) {
        await loadChildren(n, true);
        await walk(n.children);
      } else if (n.dir) {
        n.children = null;
      }
    }
  };
  await walk(tree.roots);
  const visible = new Set();
  flatten().forEach((n) => visible.add(n.path));
  for (const p of [...tree.sel]) if (!visible.has(p)) tree.sel.delete(p);
  renderTree();
}

function flatten() {
  const out = [];
  const walk = (nodes) => {
    for (const n of nodes) {
      out.push(n);
      if (n.expanded && n.children) walk(n.children);
    }
  };
  walk(tree.roots);
  return out;
}

function renderTree() {
  tree.flat = flatten();
  const el = $("#tree");
  const frag = document.createDocumentFragment();
  tree.flat.forEach((n, i) => {
    const row = document.createElement("div");
    row.className = "row-item" + (n.dir ? " dirent" : "") + (tree.sel.has(n.path) ? " sel" : "") + (tree.focus === n ? " focus" : "");
    row.dataset.i = i;
    row.style.paddingLeft = 4 + n.depth * 14 + "px";
    row.title = n.path;
    const tw = n.dir ? (n.loading ? "…" : n.expanded ? "▼" : "▶") : "";
    row.innerHTML = `<span class="tw">${tw}</span><span class="ico">${n.dir ? "📁" : "🎬"}</span>` +
      `<span class="nm">${esc(n.name)}</span>${n.dir ? "" : `<span class="sz">${fmtBytes(n.size)}</span>`}`;
    frag.appendChild(row);
    if (n.dir && n.expanded && n.children && n.children.length === 0 && !n.loading) {
      const e = document.createElement("div");
      e.className = "row-item loading";
      e.style.paddingLeft = 4 + (n.depth + 1) * 14 + 30 + "px";
      e.textContent = "(empty)";
      frag.appendChild(e);
    }
  });
  el.replaceChildren(frag);
  paintTree();
}

// Selection changes only touch classes, so the DOM survives between the clicks of a dblclick.
function paintTree() {
  for (const row of $("#tree").querySelectorAll(".row-item[data-i]")) {
    const n = tree.flat[+row.dataset.i];
    row.classList.toggle("sel", tree.sel.has(n.path));
    row.classList.toggle("focus", tree.focus === n);
  }
  renderSelPreview();
}

function treeSelect(node, ev) {
  if (ev && (ev.ctrlKey || ev.metaKey)) {
    tree.sel.has(node.path) ? tree.sel.delete(node.path) : tree.sel.add(node.path);
    tree.anchor = node;
  } else if (ev && ev.shiftKey && tree.anchor) {
    const a = tree.flat.indexOf(tree.anchor), b = tree.flat.indexOf(node);
    if (a >= 0 && b >= 0) {
      if (!(ev.ctrlKey || ev.metaKey)) tree.sel.clear();
      for (let i = Math.min(a, b); i <= Math.max(a, b); i++) tree.sel.add(tree.flat[i].path);
    }
  } else {
    tree.sel.clear();
    tree.sel.add(node.path);
    tree.anchor = node;
  }
  setFocus(node);
}

function setFocus(node, block = "nearest") {
  tree.focus = node;
  $("#tree-path").value = node.path;
  store.set("treePath", node.path);
  paintTree();
  const row = $(`#tree .row-item[data-i="${tree.flat.indexOf(node)}"]`);
  if (row) row.scrollIntoView({ block });
}

const normPath = (p) => {
  let s = isWin() ? p.replace(/\//g, "\\").toLowerCase() : p;
  return s.replace(/[\\/]+$/, "");
};

async function reveal(p) {
  p = p.trim();
  if (!p) return;
  const target = normPath(p), sep = config.sep;
  const under = (base) => target === base || target.startsWith(base + sep);
  let node = null;
  for (const r of tree.roots) {
    const rp = normPath(r.path);
    if (under(rp) && (!node || rp.length > normPath(node.path).length)) node = r;
  }
  if (!node) { toast("Path is not under any root"); return; }
  while (normPath(node.path) !== target) {
    if (!node.dir) break;
    node.expanded = true;
    if (node.children === null) await loadChildren(node);
    let child = node.children.find((c) => under(normPath(c.path)));
    if (!child) {
      // maybe hidden (e.g. AppData) or filtered out: look it up explicitly and splice it in
      try {
        const all = await api.get(`/api/ls?path=${encodeURIComponent(node.path)}&all=1&hidden=1`);
        const e = all.find((c) => under(normPath(c.path)));
        if (e) { child = mkNode(e, node); node.children.push(child); }
      } catch { /* fall through */ }
    }
    if (!child) break;
    node = child;
  }
  if (node.dir && !node.expanded) await toggleNode(node, true);
  tree.sel.clear();
  tree.sel.add(node.path);
  tree.anchor = node;
  setFocus(node, "start");
}

function initTree() {
  const el = $("#tree");
  el.addEventListener("mousedown", (ev) => {
    const row = ev.target.closest(".row-item[data-i]");
    if (!row) return;
    const node = tree.flat[+row.dataset.i];
    if (ev.target.classList.contains("tw")) { toggleNode(node); setFocus(node); return; }
    treeSelect(node, ev);
  });
  el.addEventListener("dblclick", (ev) => {
    const row = ev.target.closest(".row-item[data-i]");
    if (!row || ev.target.classList.contains("tw")) return;
    const node = tree.flat[+row.dataset.i];
    if (node.dir) toggleNode(node);
  });
  el.addEventListener("keydown", (ev) => {
    const f = tree.focus, i = tree.flat.indexOf(f);
    const go = (n) => { if (!n) return; ev.shiftKey ? treeSelect(n, { shiftKey: true }) : (ev.ctrlKey ? setFocus(n) : treeSelect(n)); };
    switch (ev.key) {
      case "ArrowDown": go(tree.flat[Math.min(tree.flat.length - 1, i + 1)] || tree.flat[0]); break;
      case "ArrowUp": go(tree.flat[Math.max(0, i - 1)]); break;
      case "Home": go(tree.flat[0]); break;
      case "End": go(tree.flat[tree.flat.length - 1]); break;
      case "ArrowRight":
        if (f && f.dir && !f.expanded) toggleNode(f, true);
        else if (f && f.expanded && f.children && f.children.length) go(f.children[0]);
        break;
      case "ArrowLeft":
        if (f && f.dir && f.expanded) toggleNode(f, false);
        else if (f && f.parent) go(f.parent);
        break;
      case " ":
        if (f) treeSelect(f, { ctrlKey: true });
        break;
      case "Enter": addJobs(); break;
      case "a":
        if (!(ev.ctrlKey || ev.metaKey)) return;
        tree.flat.forEach((n) => tree.sel.add(n.path));
        paintTree();
        break;
      case "Escape": tree.sel.clear(); paintTree(); break;
      default: return;
    }
    ev.preventDefault();
  });
  $("#tree-path").addEventListener("keydown", (ev) => { if (ev.key === "Enter") reveal(ev.target.value); });
  $("#tree-refresh").addEventListener("click", refreshTree);
  for (const id of ["tree-all", "tree-hidden"]) {
    const cb = $("#" + id);
    cb.checked = store.get(id, false);
    cb.addEventListener("change", () => { store.set(id, cb.checked); refreshTree(); });
  }
}

// ---------------------------------------------------------------- target / add

const FORM_FIELDS = ["dest", "preset", "ext", "existing", "extra", "keep"];

function initForm() {
  for (const id of FORM_FIELDS) {
    const el = $("#" + id);
    const v = store.get("form." + id, null);
    if (v !== null) el.type === "checkbox" ? (el.checked = v) : (el.value = v);
    el.addEventListener("change", () => store.set("form." + id, el.type === "checkbox" ? el.checked : el.value));
  }
  if (!$("#dest").value && config.dest) $("#dest").value = config.dest;
  $("#dest-from-tree").addEventListener("click", () => {
    const f = tree.focus;
    if (!f) return toast("Focus a folder in the file browser first");
    const dir = f.dir ? f.path : f.parent ? f.parent.path : f.path;
    $("#dest").value = dir;
    store.set("form.dest", dir);
  });
  $("#preset-refresh").addEventListener("click", () => loadPresets(true));
  $("#add").addEventListener("click", addJobs);
  $("#hb-cmd").textContent = (config.hb_cmd || []).join(" ");
  $("#hb-cmd").title = "HandBrake command: " + $("#hb-cmd").textContent;
}

async function loadPresets(refresh = false) {
  const sel = $("#preset");
  const want = store.get("form.preset", null) ?? (sel.value || config.preset || "");
  sel.innerHTML = `<option value="">loading…</option>`;
  try {
    const presets = await api.get("/api/presets" + (refresh ? "?refresh=1" : ""));
    const groups = new Map();
    for (const p of presets) {
      if (!groups.has(p.category)) groups.set(p.category, []);
      groups.get(p.category).push(p);
    }
    // custom presets first: that's what people batch with
    const cats = [...groups.keys()].sort((a, b) => (b === "Custom Presets") - (a === "Custom Presets"));
    sel.innerHTML = `<option value="">(CLI defaults, no preset)</option>` + cats.map((c) =>
      `<optgroup label="${esc(c)}">` + groups.get(c).map((p) =>
        `<option value="${esc(p.name)}" title="${esc(p.description)}">${esc(p.name)}</option>`).join("") + `</optgroup>`).join("");
    sel.value = want;
    if (sel.value !== want) sel.value = "";
  } catch (err) {
    sel.innerHTML = `<option value="">(failed to load presets)</option>`;
    toast("Presets: " + err.message);
  }
}

function renderSelPreview() {
  const paths = [...tree.sel];
  const btn = $("#add");
  btn.disabled = paths.length === 0;
  btn.textContent = paths.length ? `Add ${paths.length} item${paths.length > 1 ? "s" : ""} to queue` : "Add to queue";
  const shown = paths.slice(0, 200).map((p) => (tree.flat.find((n) => n.path === p)?.dir ? "📁 " : "🎬 ") + p);
  if (paths.length > 200) shown.push(`… and ${paths.length - 200} more`);
  $("#sel-preview").textContent = paths.length ? shown.join("\n") : "Select files or folders in the file browser (Ctrl/Shift for multiple). Folders add their videos recursively.";
}

async function addJobs() {
  const paths = [...tree.sel];
  if (!paths.length) return;
  const dest = $("#dest").value.trim();
  if (!dest) { toast("Set a target directory first"); $("#dest").focus(); return; }
  const btn = $("#add");
  btn.disabled = true;
  try {
    const r = await api.post("/api/jobs", {
      paths, dest, preset: $("#preset").value, ext: $("#ext").value, existing: $("#existing").value,
      keep_structure: $("#keep").checked, extra_args: $("#extra").value,
    });
    const msg = $("#add-msg");
    msg.textContent = `Added ${r.added} job${r.added === 1 ? "" : "s"}` + (r.skipped.length ? `, skipped ${r.skipped.length}` : "");
    msg.title = r.skipped.map((s) => `${s.src}: ${s.reason}`).join("\n");
    msg.className = r.skipped.length ? "err" : "";
    if (r.added) { tree.sel.clear(); paintTree(); }
    poll();
  } catch (err) {
    toast("Add failed: " + err.message);
  } finally {
    renderSelPreview();
  }
}

// ---------------------------------------------------------------- queue

const queue = { sel: new Set(), anchor: null, focus: null, rows: new Map(), dragIds: null, pendingSingle: null };

function renderQueue() {
  const list = $("#queue");
  const jobs = state.jobs;
  for (const id of [...queue.sel]) if (!jobsById.has(id)) queue.sel.delete(id);
  if (!jobs.length) {
    list.innerHTML = `<div class="empty">Queue is empty. Select files above and add them.</div>`;
    queue.rows.clear();
    return;
  }
  list.querySelector(".empty")?.remove();
  const seen = new Set();
  let prev = null;
  for (const j of jobs) {
    let el = queue.rows.get(j.id);
    if (!el) {
      el = document.createElement("div");
      el.dataset.id = j.id;
      el.innerHTML = `<span class="pct"></span><span class="nm"></span>`;
      queue.rows.set(j.id, el);
    }
    const pct = (j.status === "done" ? 100 : Math.floor(100 * j.progress)) + "%";
    if (el.firstChild.textContent !== pct) el.firstChild.textContent = pct;
    if (el.lastChild.textContent !== j.name) el.lastChild.textContent = j.name;
    el.className = `row-item st-${j.status}` + (queue.sel.has(j.id) ? " sel" : "") + (queue.focus === j.id ? " focus" : "");
    el.draggable = j.status !== "running";
    el.style.setProperty("--p", (100 * j.progress).toFixed(2) + "%");
    el.title = `${j.src}\n→ ${j.dst}` + (j.error ? `\n✗ ${j.error}` : "");
    const want = prev ? prev.nextSibling : list.firstChild;
    if (want !== el) list.insertBefore(el, want);
    prev = el;
    seen.add(j.id);
  }
  for (const [id, el] of queue.rows) if (!seen.has(id)) { el.remove(); queue.rows.delete(id); }

  const c = state.summary.counts;
  $("#queue-counts").innerHTML = `<span>${jobs.length} jobs</span><span class="st-queued">${c.queued} queued</span>` +
    `<span class="st-running">${c.running} running</span><span class="st-done">${c.done} done</span><span class="st-failed">${c.failed} failed</span>`;
}

function queueSelect(id, ev) {
  const ids = state.jobs.map((j) => j.id);
  if (ev.ctrlKey || ev.metaKey) {
    queue.sel.has(id) ? queue.sel.delete(id) : queue.sel.add(id);
    queue.anchor = id;
  } else if (ev.shiftKey && queue.anchor && ids.includes(queue.anchor)) {
    const a = ids.indexOf(queue.anchor), b = ids.indexOf(id);
    queue.sel.clear();
    for (let i = Math.min(a, b); i <= Math.max(a, b); i++) queue.sel.add(ids[i]);
  } else {
    queue.sel.clear();
    queue.sel.add(id);
    queue.anchor = id;
  }
  queue.focus = id;
  renderAll();
  queue.rows.get(id)?.scrollIntoView({ block: "nearest" });
}

async function moveJobs(ids, before) {
  if (!ids.length) return;
  // optimistic local reorder, the next poll confirms it
  const set = new Set(ids);
  const moving = state.jobs.filter((j) => set.has(j.id));
  const rest = state.jobs.filter((j) => !set.has(j.id));
  let idx = rest.findIndex((j) => j.id === before);
  if (idx < 0) idx = rest.length;
  state.jobs = [...rest.slice(0, idx), ...moving, ...rest.slice(idx)];
  renderQueue();
  try { await api.post("/api/move", { ids, before }); } catch (err) { toast("Move failed: " + err.message); }
  poll();
}

function movableSelection() {
  return state.jobs.filter((j) => queue.sel.has(j.id) && j.status !== "running").map((j) => j.id);
}

function nudgeSelection(dir) {
  const ids = movableSelection();
  if (!ids.length) return;
  const order = state.jobs.map((j) => j.id);
  const set = new Set(ids);
  if (dir < 0) {
    const first = order.findIndex((id) => set.has(id));
    if (first <= 0) return;
    moveJobs(ids, order[first - 1]);
  } else {
    let last = -1;
    order.forEach((id, i) => { if (set.has(id)) last = i; });
    const after = order.slice(last + 1).filter((id) => !set.has(id));
    if (!after.length) return;
    moveJobs(ids, after[1] ?? null);
  }
}

async function removeSelected() {
  const sel = state.jobs.filter((j) => queue.sel.has(j.id));
  if (!sel.length) return;
  const unfinished = sel.filter((j) => j.status !== "done");
  if (unfinished.length) {
    const running = sel.some((j) => j.status === "running");
    const msg = `Remove ${sel.length} job${sel.length > 1 ? "s" : ""}?\n\n${unfinished.length} not completed` +
      (running ? "; the running encode will be aborted." : ".");
    if (!confirm(msg)) return;
  }
  try { await api.post("/api/remove", { ids: sel.map((j) => j.id) }); } catch (err) { toast(err.message); }
  queue.sel.clear();
  poll();
}

function clearDropMarks() {
  document.querySelectorAll(".drop-before,.drop-after").forEach((e) => e.classList.remove("drop-before", "drop-after"));
  $("#queue").classList.remove("drop-end");
}

function initQueue() {
  const list = $("#queue");
  list.addEventListener("mousedown", (ev) => {
    const row = ev.target.closest(".row-item[data-id]");
    if (!row) { if (!ev.ctrlKey && !ev.shiftKey) { queue.sel.clear(); renderAll(); } return; }
    const id = row.dataset.id;
    queue.pendingSingle = null;
    if (!ev.ctrlKey && !ev.metaKey && !ev.shiftKey && queue.sel.has(id)) {
      queue.pendingSingle = id; // decide on click, so a multi-selection can still be dragged
      queue.focus = id;
      return;
    }
    queueSelect(id, ev);
  });
  list.addEventListener("click", (ev) => {
    if (queue.pendingSingle) { queueSelect(queue.pendingSingle, {}); queue.pendingSingle = null; }
  });
  list.addEventListener("keydown", (ev) => {
    const ids = state.jobs.map((j) => j.id);
    const i = ids.indexOf(queue.focus);
    switch (ev.key) {
      case "Delete": removeSelected(); break;
      case "Escape": queue.sel.clear(); renderAll(); break;
      case "a":
        if (!(ev.ctrlKey || ev.metaKey)) return;
        ids.forEach((id) => queue.sel.add(id));
        renderAll();
        break;
      case "ArrowUp":
      case "ArrowDown": {
        const d = ev.key === "ArrowUp" ? -1 : 1;
        if (ev.altKey) { nudgeSelection(d); break; }
        const n = ids[Math.max(0, Math.min(ids.length - 1, i < 0 ? 0 : i + d))];
        if (n) queueSelect(n, { shiftKey: ev.shiftKey });
        break;
      }
      default: return;
    }
    ev.preventDefault();
  });

  list.addEventListener("dragstart", (ev) => {
    const row = ev.target.closest(".row-item[data-id]");
    const job = row && jobsById.get(row.dataset.id);
    if (!job || job.status === "running") { ev.preventDefault(); return; }
    queue.pendingSingle = null;
    if (!queue.sel.has(job.id)) { queue.sel.clear(); queue.sel.add(job.id); queue.anchor = job.id; renderAll(); }
    queue.dragIds = movableSelection();
    ev.dataTransfer.effectAllowed = "move";
    ev.dataTransfer.setData("text/plain", queue.dragIds.length + " jobs");
  });
  list.addEventListener("dragover", (ev) => {
    if (!queue.dragIds) return;
    ev.preventDefault();
    ev.dataTransfer.dropEffect = "move";
    clearDropMarks();
    const row = ev.target.closest(".row-item[data-id]");
    if (!row) { list.classList.add("drop-end"); return; }
    const r = row.getBoundingClientRect();
    row.classList.add(ev.clientY > r.top + r.height / 2 ? "drop-after" : "drop-before");
  });
  list.addEventListener("dragleave", (ev) => { if (!list.contains(ev.relatedTarget)) clearDropMarks(); });
  list.addEventListener("drop", (ev) => {
    if (!queue.dragIds) return;
    ev.preventDefault();
    const ids = queue.dragIds;
    const set = new Set(ids);
    const row = ev.target.closest(".row-item[data-id]");
    let before = null;
    if (row) {
      const order = state.jobs.map((j) => j.id);
      const r = row.getBoundingClientRect();
      let k = order.indexOf(row.dataset.id) + (ev.clientY > r.top + r.height / 2 ? 1 : 0);
      while (k < order.length && set.has(order[k])) k++;
      before = order[k] ?? null;
    }
    clearDropMarks();
    queue.dragIds = null;
    moveJobs(ids, before);
  });
  list.addEventListener("dragend", () => { queue.dragIds = null; clearDropMarks(); });
}

// ---------------------------------------------------------------- status

function kv(k, v, cls = "") { return `<div class="k">${esc(k)}</div><div class="v ${cls}">${v}</div>`; }
const h = (t) => `<div class="h">${t}</div>`;
const st = (s, txt = s) => `<span class="st-${s}">${esc(txt)}</span>`;

function renderToolbar() {
  const s = state.state, cur = state.current;
  const sel = state.jobs.filter((j) => queue.sel.has(j.id));
  const start = $("#btn-start");
  start.disabled = s === "running";
  start.textContent = s === "paused" ? "▶ Resume" : "▶ Start";
  $("#btn-pause").disabled = s !== "running";
  const sa = $("#btn-stopafter");
  sa.disabled = s === "idle";
  sa.classList.toggle("on", !!state.stop_after_current);
  $("#btn-abort").disabled = !cur;
  $("#btn-retry").disabled = !sel.some((j) => j.status === "done" || j.status === "failed");
  $("#btn-remove").disabled = !sel.length;
  $("#btn-clear").disabled = !state.summary.counts.done;
  const badge = $("#run-state");
  badge.className = "badge " + s;
  badge.textContent = s.toUpperCase() + (state.stop_after_current ? " · STOP AFTER CURRENT" : "");
}

function renderOverview() {
  const S = state.summary, c = S.counts, now = state.now;
  const cur = state.current && jobsById.get(state.current);
  let html = h("Queue");
  html += kv("State", st(state.state === "idle" ? "queued" : "running", state.state) + (cur ? " — " + esc(cur.name) : ""));
  if (state.session_started) {
    const end = state.state === "idle" && state.session_finished ? state.session_finished : now;
    html += kv("Started", `${fmtClock(state.session_started)} <span class="dim">(${fmtDur(end - state.session_started)} elapsed${state.state === "idle" && state.session_finished ? ", ended " + fmtClock(state.session_finished) : ""})</span>`);
  }
  html += kv("Jobs", `${S.total} — ${st("queued", c.queued + " queued")} · ${st("running", c.running + " running")} · ${st("done", c.done + " done")} · ${st("failed", c.failed + " failed")}`);
  html += kv("Progress", bar(S.progress, state.state !== "idle"));
  html += kv("Size", `${fmtBytes(S.size_done)} / ${fmtBytes(S.size_total)} <span class="dim">(${fmtPct(S.size_total ? S.size_done / S.size_total : 0)})</span>`);
  html += kv("Length", `${fmtDur(S.dur_done)} / ${fmtDur(S.dur_total)} <span class="dim">(${fmtPct(S.dur_total ? S.dur_done / S.dur_total : 0)})` +
    (S.dur_unknown ? `, ${S.dur_unknown} unknown` : "") + `</span>`);
  html += kv("Encode time", fmtDur(S.encode_time));
  html += kv("ETA", S.eta != null && (c.queued || c.running) ? `${fmtDur(S.eta)} <span class="dim">(≈ ${fmtClock(now + S.eta)})</span>` : "–");
  if (S.src_size_done) html += kv("Output", `${fmtBytes(S.out_size_done)} from ${fmtBytes(S.src_size_done)} <span class="dim">(${fmtPct(S.out_size_done / S.src_size_done)} of source)</span>`);
  $("#overview").innerHTML = html;
}

const selinfo = { kv: null, log: null, logFor: null, logAt: 0 };

function renderSelInfo() {
  const sel = state.jobs.filter((j) => queue.sel.has(j.id));
  if (!sel.length) { selinfo.kv.innerHTML = ""; selinfo.log.hidden = true; selinfo.logFor = null; return; }
  let html = "";
  if (sel.length === 1) {
    const j = sel[0];
    html += h(`Job — ${st(j.status)}`);
    html += kv("Name", esc(j.name));
    if (j.error) html += kv("Error", esc(j.error), "err");
    html += kv("Progress", bar(j.progress, j.status === "running") + (j.pass_count > 1 ? `<span class="dim">pass ${j.pass_no}/${j.pass_count}</span>` : ""));
    html += kv("Source", esc(j.src));
    html += kv("Target", esc(j.dst));
    html += kv("Preset", esc(j.preset || "(CLI defaults)") + (j.extra_args.length ? ` <span class="dim">+ ${esc(j.extra_args.join(" "))}</span>` : ""));
    html += kv("Source size", fmtBytes(j.size));
    html += kv("Length", fmtDur(j.duration));
    if (j.status === "running" || j.out_size) {
      let out = fmtBytes(j.out_size);
      if (j.status === "running" && j.progress > 0.02) out += ` <span class="dim">(est. final ≈ ${fmtBytes(j.out_size / j.progress)})</span>`;
      if (j.status === "done" && j.size) out += ` <span class="dim">(${fmtPct(j.out_size / j.size)} of source)</span>`;
      html += kv("Output size", out);
    }
    if (j.elapsed) html += kv("Elapsed", fmtDur(j.elapsed));
    if (j.status === "running") html += kv("ETA", j.eta != null ? `${fmtDur(j.eta)} <span class="dim">(≈ ${fmtClock(state.now + j.eta)})</span>` : "–");
    if (j.elapsed && j.progress > 0) {
      const parts = [];
      if (j.fps) parts.push(`${j.fps.toFixed(1)} fps`);
      if (j.fps_avg) parts.push(`avg ${j.fps_avg.toFixed(1)} fps`);
      if (j.duration) parts.push(`${(j.progress * j.duration / j.elapsed).toFixed(2)}× realtime`);
      if (parts.length) html += kv("Speed", parts.join(" · "));
    }
    html += kv("Added", fmtClock(j.added_at));
    if (j.started_at) html += kv("Started", fmtClock(j.started_at));
    if (j.finished_at && j.status !== "running") html += kv("Finished", fmtClock(j.finished_at));
    if (j.exit_code != null) html += kv("Exit code", j.exit_code);
    selinfo.kv.innerHTML = html;
    updateLog(j);
  } else {
    const c = { queued: 0, running: 0, done: 0, failed: 0 };
    sel.forEach((j) => c[j.status]++);
    const size = sel.reduce((a, j) => a + j.size, 0);
    const dur = sel.reduce((a, j) => a + (j.duration || 0), 0);
    const unknown = sel.filter((j) => !j.duration).length;
    const prog = size ? sel.reduce((a, j) => a + j.size * j.progress, 0) / size : 0;
    const done = sel.filter((j) => j.status === "done");
    const outDone = done.reduce((a, j) => a + j.out_size, 0), srcDone = done.reduce((a, j) => a + j.size, 0);
    html += h(`Selection — ${sel.length} jobs`);
    html += kv("Status", `${st("queued", c.queued + " queued")} · ${st("running", c.running + " running")} · ${st("done", c.done + " done")} · ${st("failed", c.failed + " failed")}`);
    html += kv("Progress", bar(prog, c.running > 0));
    html += kv("Source size", fmtBytes(size));
    html += kv("Length", fmtDur(dur) + (unknown ? ` <span class="dim">(${unknown} unknown)</span>` : ""));
    if (done.length) html += kv("Output", `${fmtBytes(outDone)} from ${fmtBytes(srcDone)} <span class="dim">(${fmtPct(outDone / srcDone)} of source)</span>`);
    const enc = sel.reduce((a, j) => a + j.elapsed, 0);
    if (enc) html += kv("Encode time", fmtDur(enc));
    const presets = [...new Set(sel.map((j) => j.preset || "(CLI defaults)"))];
    html += kv("Preset" + (presets.length > 1 ? "s" : ""), esc(presets.join(", ")));
    selinfo.kv.innerHTML = html;
    selinfo.log.hidden = true;
    selinfo.logFor = null;
  }
}

async function updateLog(j) {
  const now = Date.now();
  const changed = selinfo.logFor !== j.id;
  if (!changed && (j.status !== "running" || now - selinfo.logAt < 2000)) return;
  if (j.status === "queued" && !j.started_at) { selinfo.log.hidden = true; selinfo.logFor = j.id; return; }
  selinfo.logFor = j.id;
  selinfo.logAt = now;
  let lines;
  try { lines = await api.get(`/api/log?id=${j.id}&lines=120`); } catch { return; }
  if (selinfo.logFor !== j.id) return;
  const pre = selinfo.log;
  const stick = changed || pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 4;
  pre.hidden = !lines.length;
  pre.textContent = lines.map((l) => l.replace(/\n$/, "")).join("\n");
  if (stick) pre.scrollTop = pre.scrollHeight;
}

function initStatus() {
  selinfo.kv = document.createElement("div");
  selinfo.kv.className = "kv";
  selinfo.log = document.createElement("pre");
  selinfo.log.hidden = true;
  $("#selinfo").append(selinfo.kv, selinfo.log);
  const ctl = (action) => async () => {
    try { await api.post("/api/control", { action }); } catch (err) { toast(err.message); }
    poll();
  };
  $("#btn-start").addEventListener("click", ctl("start"));
  $("#btn-pause").addEventListener("click", ctl("pause"));
  $("#btn-stopafter").addEventListener("click", ctl("stop_after"));
  $("#btn-abort").addEventListener("click", () => {
    const cur = state.current && jobsById.get(state.current);
    if (cur && confirm(`Abort "${cur.name}" and stop the queue?`)) ctl("abort")();
  });
  $("#btn-retry").addEventListener("click", async () => {
    const ids = state.jobs.filter((j) => queue.sel.has(j.id) && (j.status === "done" || j.status === "failed")).map((j) => j.id);
    const redo = state.jobs.filter((j) => ids.includes(j.id) && j.status === "done").length;
    if (redo && !confirm(`${redo} selected job(s) already finished; re-encode and overwrite their output?`)) return;
    try { await api.post("/api/retry", { ids }); } catch (err) { toast(err.message); }
    poll();
  });
  $("#btn-remove").addEventListener("click", removeSelected);
  $("#btn-clear").addEventListener("click", async () => {
    try { await api.post("/api/clear_done"); } catch (err) { toast(err.message); }
    poll();
  });
}

// ---------------------------------------------------------------- splitters

function initSplitters() {
  const app = $("#app");
  const col = store.get("split.col", null), row = store.get("split.row", null);
  if (col) app.style.setProperty("--col", col);
  if (row) app.style.setProperty("--row", row);
  const drag = (el, prop, axis) => el.addEventListener("mousedown", (ev) => {
    ev.preventDefault();
    document.body.classList.add("dragging-split");
    const r = app.getBoundingClientRect();
    const move = (e) => {
      const f = axis === "x" ? (e.clientX - r.left) / r.width : (e.clientY - r.top) / r.height;
      const v = (100 * Math.min(0.85, Math.max(0.15, f))).toFixed(2) + "%";
      app.style.setProperty(prop, v);
      store.set(prop === "--col" ? "split.col" : "split.row", v);
    };
    const up = () => {
      document.removeEventListener("mousemove", move);
      document.removeEventListener("mouseup", up);
      document.body.classList.remove("dragging-split");
    };
    document.addEventListener("mousemove", move);
    document.addEventListener("mouseup", up);
  });
  drag($("#split-v"), "--col", "x");
  drag($("#split-h"), "--row", "y");
}

// ---------------------------------------------------------------- polling

function renderAll() {
  if (!state.summary) return;
  renderQueue();
  renderToolbar();
  renderOverview();
  renderSelInfo();
}

let polling = false, offline = false;
async function poll() {
  if (polling) return;
  polling = true;
  try {
    state = await api.get("/api/state");
    jobsById = new Map(state.jobs.map((j) => [j.id, j]));
    if (offline) { offline = false; $("#toast").classList.remove("show"); }
    renderAll();
  } catch (err) {
    if (!offline) { offline = true; toast("Lost connection to hbmon server…"); }
  } finally {
    polling = false;
  }
}

async function main() {
  config = await api.get("/api/config");
  tree.roots = config.roots.map((r) => mkNode(r, null));
  initTree();
  initForm();
  initQueue();
  initStatus();
  initSplitters();
  renderTree();
  loadPresets();
  poll();
  setInterval(poll, 1000);
  const last = store.get("treePath", null);
  if (last) reveal(last); else if (tree.roots[0]) toggleNode(tree.roots[0], true);
}

main().catch((err) => toast("Startup failed: " + err.message));
