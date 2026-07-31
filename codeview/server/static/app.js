const BRANCHES = [
  { kind: "contains", label: "members" },
  { kind: "called_by", label: "callers" },
  { kind: "calls", label: "callees" },
  { kind: "parent_class", label: "parents" },
  { kind: "child_class", label: "children" },
  { kind: "overrides", label: "overrides" },
  { kind: "overridden_by", label: "overridden by" },
  { kind: "implements", label: "implements" },
  { kind: "implemented_by", label: "implemented by" },
  { kind: "references", label: "references" },
];

const state = {
  history: [],
  historyIndex: -1,
  selectedId: null,
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
  els.crumbs.textContent = state.history
    .slice(0, state.historyIndex + 1)
    .map((s) => s.name)
    .join(" / ");
}

function pushHistory(symbol) {
  const step = { id: symbol.id, name: symbol.name };
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
  btn.innerHTML = `<span>${esc(symbol.name)}</span> <span class="kind">${esc(symbol.kind)}</span> <span class="loc">${esc(symbol.location.path)}:${symbol.location.line}</span>`;
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    selectSymbol(symbol, true);
  });
  return btn;
}

function makeSymbolNode(symbol) {
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
        await loadBranches(symbol, children);
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

async function loadBranches(symbol, container) {
  container.innerHTML = "";
  let any = false;

  for (const branch of BRANCHES) {
    const data = await api("/api/expand", {
      method: "POST",
      body: JSON.stringify({ symbol_id: symbol.id, kind: branch.kind }),
    });
    const relations = (data.relations || []).filter((r) => r.to_symbol);
    // For contains, skip pointing back at modules noise if any
    const items = relations
      .map((r) => r.to_symbol)
      .filter((s) => s && s.id !== symbol.id);

    // dedupe by id
    const seen = new Set();
    const unique = [];
    for (const s of items) {
      if (seen.has(s.id)) continue;
      seen.add(s.id);
      unique.push(s);
    }
    if (!unique.length) continue;

    any = true;
    const groupLi = document.createElement("li");
    const label = document.createElement("div");
    label.className = "group";
    label.textContent = `${branch.label} (${unique.length})`;
    const list = document.createElement("ul");
    unique.forEach((s) => list.appendChild(makeSymbolNode(s)));
    groupLi.appendChild(label);
    groupLi.appendChild(list);
    container.appendChild(groupLi);
  }

  if (!any) {
    container.innerHTML = `<li class="empty">no relations</li>`;
  }
}

async function loadTree() {
  const q = els.filter.value.trim();
  const data = await api(`/api/tree?q=${encodeURIComponent(q)}`);
  els.tree.innerHTML = "";
  if (!data.results.length) {
    els.tree.innerHTML = `<li class="empty">no top-level symbols</li>`;
    return;
  }
  data.results.forEach((s) => els.tree.appendChild(makeSymbolNode(s)));
}

async function init() {
  try {
    const stats = await api("/api/stats");
    if (!stats.has_index) {
      els.status.textContent = "no index";
      return;
    }
    els.status.textContent = `${stats.symbol_count} symbols · ${stats.root}`;
    await loadTree();
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
