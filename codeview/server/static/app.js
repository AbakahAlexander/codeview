const BRANCHES = [
  { kind: "contains", label: "members" },
  { kind: "called_by", label: "callers" },
  { kind: "calls", label: "callees" },
];

const state = {
  history: [],
  historyIndex: -1,
  selectedId: null,
  browsePath: "",
  mode: "symbols",
};

const els = {
  status: document.getElementById("status"),
  filter: document.getElementById("filter"),
  tree: document.getElementById("tree"),
  src: document.getElementById("src"),
  srcMeta: document.getElementById("srcMeta"),
  crumbs: document.getElementById("crumbs"),
  back: document.getElementById("back"),
  forward: document.getElementById("forward"),
};

async function api(path, options) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || res.statusText);
  return data;
}

function esc(s) {
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function updateNav() {
  els.back.disabled = state.historyIndex <= 0;
  els.forward.disabled = state.historyIndex >= state.history.length - 1;
  const current = state.history[state.historyIndex];
  els.crumbs.textContent = current
    ? `${current.name} · ${current.path}${current.line ? ":" + current.line : ""}`
    : state.browsePath
      ? `path: ${state.browsePath || "."}`
      : "";
}

function pushHistory(symbol) {
  const step = {
    id: symbol.id,
    name: symbol.name,
    path: symbol.location.path,
    line: symbol.location.line,
  };
  state.history = state.history.slice(0, state.historyIndex + 1);
  if (state.history[state.historyIndex]?.id === step.id) {
    updateNav();
    return;
  }
  state.history.push(step);
  state.historyIndex = state.history.length - 1;
  updateNav();
}

async function selectSymbol(symbol, record = true) {
  state.selectedId = symbol.id;
  document.querySelectorAll(".sym.active").forEach((el) => el.classList.remove("active"));
  document.querySelectorAll(`.sym[data-id="${symbol.id}"]`).forEach((el) => el.classList.add("active"));
  if (record) pushHistory(symbol);
  if (symbol.kind === "directory") {
    els.srcMeta.textContent = symbol.location.path;
    els.src.textContent = "(directory — expand with + to browse)";
    return;
  }
  const snippet = await api(`/api/source/${symbol.id}`);
  els.srcMeta.textContent = `${snippet.path}:${snippet.start_line}-${snippet.end_line}`;
  const lines = (snippet.text || "").split("\n");
  els.src.innerHTML = lines
    .map((line, i) => {
      const n = snippet.start_line + i;
      const hl = n === snippet.highlight_line ? " hl" : "";
      return `<span class="line${hl}"><span class="ln">${n}</span>${esc(line)}</span>`;
    })
    .join("");
}

function symbolButton(symbol) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "sym" + (state.selectedId === symbol.id ? " active" : "");
  btn.dataset.id = symbol.id;
  btn.innerHTML = `<span>${esc(symbol.name)}</span> <span class="kind">${esc(symbol.kind)}</span> <span class="loc">${esc(symbol.location.path)}${symbol.kind === "directory" ? "" : ":" + symbol.location.line}</span>`;
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    selectSymbol(symbol, true);
  });
  return btn;
}

function makeSymbolNode(symbol, ancestors = []) {
  const path = ancestors.includes(symbol.id) ? ancestors : [...ancestors, symbol.id];
  const li = document.createElement("li");
  const row = document.createElement("div");
  row.className = "row";

  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "toggle";
  toggle.textContent = "+";
  toggle.title = "expand";

  const children = document.createElement("ul");
  children.hidden = true;
  let loaded = false;
  let open = false;

  toggle.addEventListener("click", async (e) => {
    e.stopPropagation();
    if (open) {
      open = false;
      children.hidden = true;
      toggle.textContent = "+";
      return;
    }
    open = true;
    children.hidden = false;
    toggle.textContent = "−";
    if (!loaded) {
      loaded = true;
      children.innerHTML = `<li class="empty">loading…</li>`;
      try {
        if (symbol.kind === "directory") {
          await loadDirectoryChildren(symbol, children, path);
        } else {
          await loadBranches(symbol, children, path);
        }
      } catch (err) {
        children.innerHTML = `<li class="empty">${esc(err.message)}</li>`;
      }
    }
  });

  row.appendChild(toggle);
  row.appendChild(symbolButton(symbol));
  li.appendChild(row);
  li.appendChild(children);
  return li;
}

async function loadDirectoryChildren(symbol, container, ancestors) {
  const rel = symbol.location.path === "." ? "" : symbol.location.path;
  const data = await api(`/api/tree?path=${encodeURIComponent(rel)}`);
  container.innerHTML = "";
  if (!data.results.length) {
    container.innerHTML = `<li class="empty">empty</li>`;
    return;
  }
  data.results.forEach((s) => container.appendChild(makeSymbolNode(s, ancestors)));
}

async function loadBranches(symbol, container, ancestors = []) {
  container.innerHTML = "";
  const blocked = new Set(ancestors);

  // For files, members = parse file contents.
  const kinds =
    symbol.kind === "module" && symbol.signature === "file"
      ? [{ kind: "contains", label: "symbols" }]
      : BRANCHES;

  const results = await Promise.all(
    kinds.map(async (branch) => {
      const data = await api("/api/expand", {
        method: "POST",
        body: JSON.stringify({ symbol_id: symbol.id, kind: branch.kind }),
      });
      const relations = (data.relations || []).filter((r) => r.to_symbol);
      const seen = new Set();
      const unique = [];
      const back = [];
      const perCallSite = branch.kind === "calls" || branch.kind === "called_by";
      for (const r of relations) {
        const s = r.to_symbol;
        if (!s || s.id === symbol.id) continue;
        // Callers/callees: one row per call site (same symbol on two lines stays visible).
        const site = r.location || {};
        const key = perCallSite
          ? `${s.id}@${site.path || ""}:${site.line || ""}`
          : s.id;
        if (seen.has(key)) continue;
        seen.add(key);
        const display = perCallSite && site.line
          ? {
              ...s,
              location: {
                ...s.location,
                path: site.path || s.location.path,
                line: site.line,
                column: site.column ?? 0,
              },
            }
          : s;
        if (blocked.has(s.id)) back.push(display);
        else unique.push(display);
      }
      return {
        branch,
        unique,
        back,
        total: data.total ?? unique.length + back.length,
        truncated: !!data.truncated,
      };
    })
  );

  let any = false;
  for (const { branch, unique, back, total, truncated } of results) {
    if (!unique.length && !back.length) continue;
    any = true;
    const groupLi = document.createElement("li");
    const label = document.createElement("div");
    label.className = "group";
    const shown = unique.length + back.length;
    label.textContent = truncated
      ? `${branch.label} (${shown} of ${total})`
      : `${branch.label} (${shown})`;
    const list = document.createElement("ul");
    unique.forEach((s) => list.appendChild(makeSymbolNode(s, ancestors)));
    // Keep back-edges visible (e.g. A→B→A) but don't offer another expand cycle.
    back.forEach((s) => {
      const li = document.createElement("li");
      const row = document.createElement("div");
      row.className = "row";
      const mark = document.createElement("span");
      mark.className = "toggle";
      mark.textContent = "·";
      mark.title = "already in path";
      row.appendChild(mark);
      row.appendChild(symbolButton(s));
      li.appendChild(row);
      list.appendChild(li);
    });
    groupLi.appendChild(label);
    groupLi.appendChild(list);
    container.appendChild(groupLi);
  }

  if (!any) {
    container.innerHTML = `<li class="empty">no members, callers, or callees in index</li>`;
  }
}

async function loadTree() {
  const q = els.filter.value.trim();
  const qs = q
    ? `/api/tree?q=${encodeURIComponent(q)}`
    : `/api/tree?path=${encodeURIComponent(state.browsePath || "")}`;
  const data = await api(qs);
  state.mode = data.mode || "symbols";
  els.tree.innerHTML = "";
  if (!data.results.length) {
    els.tree.innerHTML = `<li class="empty">${q ? "no matches" : "empty"}</li>`;
    return;
  }
  const entryCount = data.entry_point_count || 0;
  if (!q && (data.mode === "entrypoints" || entryCount > 0)) {
    const header = document.createElement("li");
    header.innerHTML = `<div class="group">entry points</div>`;
    els.tree.appendChild(header);
  }
  data.results.forEach((s) => els.tree.appendChild(makeSymbolNode(s)));
  updateNav();
}

async function applyIndexStatus(st) {
  if (!st) return;
  if (st.status === "indexing") {
    const pct = typeof st.percent === "number" ? st.percent : 0;
    const msg = st.message || "Indexing…";
    els.status.textContent = `${msg} ${pct}%`;
    return "indexing";
  }
  if (st.status === "error") {
    els.status.textContent = st.error || st.message || "index failed";
    return "error";
  }
  return "ready";
}

async function pollIndexUntilReady() {
  let lastReady = false;
  for (;;) {
    const st = await api("/api/index-status");
    const phase = await applyIndexStatus(st);
    if (phase === "ready") {
      if (!lastReady) {
        lastReady = true;
        const stats = await api("/api/stats");
        if (stats.has_index) {
          els.status.textContent = `${stats.symbol_count || 0} symbols · ${stats.root || ""}`;
          await loadTree();
        }
      }
      return;
    }
    if (phase === "error") return;
    // While indexing, show whatever graph already exists.
    if (st.has_graph) {
      await loadTree();
    } else {
      els.tree.innerHTML = `<li class="empty">${esc(st.message || "Indexing…")} ${st.percent || 0}%</li>`;
    }
    await new Promise((r) => setTimeout(r, 800));
  }
}

async function init() {
  try {
    const stats = await api("/api/stats");
    const st = stats.index_status || (await api("/api/index-status"));
    if (st && st.status === "indexing") {
      if (stats.has_index && (stats.symbol_count || 0) > 0) {
        els.status.textContent = `${stats.symbol_count} symbols · updating…`;
        await loadTree();
      }
      await pollIndexUntilReady();
      return;
    }
    if (!stats.has_index && (!st || st.status !== "ready")) {
      els.status.textContent = "no index";
      await pollIndexUntilReady();
      return;
    }
    els.status.textContent = `${stats.symbol_count || stats.file_count || 0} symbols · ${stats.root || ""}`;
    await loadTree();
    if (st && st.status === "indexing") await pollIndexUntilReady();
  } catch (err) {
    els.status.textContent = err.message;
  }

  let t = null;
  els.filter.addEventListener("input", () => {
    clearTimeout(t);
    t = setTimeout(loadTree, 150);
  });
  els.back.addEventListener("click", async () => {
    if (state.historyIndex <= 0) return;
    state.historyIndex -= 1;
    updateNav();
    const step = state.history[state.historyIndex];
    const data = await api(`/api/symbol/${step.id}`);
    await selectSymbol(data.symbol, false);
  });
  els.forward.addEventListener("click", async () => {
    if (state.historyIndex >= state.history.length - 1) return;
    state.historyIndex += 1;
    updateNav();
    const step = state.history[state.historyIndex];
    const data = await api(`/api/symbol/${step.id}`);
    await selectSymbol(data.symbol, false);
  });
}

init();
