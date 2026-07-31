const BRANCHES = [
  { kind: "called_by", label: "Callers" },
  { kind: "calls", label: "Calls" },
  { kind: "references", label: "References / usages" },
  { kind: "parent_class", label: "Parent classes" },
  { kind: "child_class", label: "Child classes" },
  { kind: "implements", label: "Implements" },
  { kind: "implemented_by", label: "Implemented by" },
  { kind: "overrides", label: "Overrides" },
  { kind: "overridden_by", label: "Overridden by" },
  { kind: "contains", label: "Contains" },
  { kind: "contained_in", label: "Contained in" },
];

const state = {
  current: null,
  history: [],
  historyIndex: -1,
  suppressHistory: false,
};

const els = {
  statusLine: document.getElementById("statusLine"),
  rootInput: document.getElementById("rootInput"),
  providerSelect: document.getElementById("providerSelect"),
  indexBtn: document.getElementById("indexBtn"),
  indexHint: document.getElementById("indexHint"),
  searchInput: document.getElementById("searchInput"),
  searchResults: document.getElementById("searchResults"),
  savedPaths: document.getElementById("savedPaths"),
  emptyState: document.getElementById("emptyState"),
  symbolView: document.getElementById("symbolView"),
  sourcePath: document.getElementById("sourcePath"),
  sourceCode: document.getElementById("sourceCode"),
  breadcrumbs: document.getElementById("breadcrumbs"),
  backBtn: document.getElementById("backBtn"),
  forwardBtn: document.getElementById("forwardBtn"),
  savePathBtn: document.getElementById("savePathBtn"),
};

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(data.detail || res.statusText || "Request failed");
  }
  return data;
}

function setStatus(text) {
  els.statusLine.textContent = text;
}

function updateNavButtons() {
  els.backBtn.disabled = state.historyIndex <= 0;
  els.forwardBtn.disabled = state.historyIndex >= state.history.length - 1;
  els.savePathBtn.disabled = state.history.length === 0;
}

function renderBreadcrumbs() {
  els.breadcrumbs.innerHTML = "";
  state.history.slice(0, state.historyIndex + 1).forEach((step, idx) => {
    if (idx > 0) {
      const sep = document.createElement("span");
      sep.className = "crumb-sep";
      sep.textContent = "/";
      els.breadcrumbs.appendChild(sep);
    }
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "crumb";
    btn.textContent = step.name;
    btn.title = step.qualname || step.name;
    btn.addEventListener("click", () => jumpToHistory(idx));
    els.breadcrumbs.appendChild(btn);
  });
}

function pushHistory(symbol) {
  if (state.suppressHistory) return;
  const step = {
    id: symbol.id,
    name: symbol.name,
    qualname: symbol.qualname,
    kind: symbol.kind,
  };
  state.history = state.history.slice(0, state.historyIndex + 1);
  state.history.push(step);
  state.historyIndex = state.history.length - 1;
  updateNavButtons();
  renderBreadcrumbs();
}

async function jumpToHistory(index) {
  if (index < 0 || index >= state.history.length) return;
  state.historyIndex = index;
  state.suppressHistory = true;
  updateNavButtons();
  renderBreadcrumbs();
  await openSymbol(state.history[index].id, false);
  state.suppressHistory = false;
}

async function refreshStats() {
  const stats = await api("/api/stats");
  if (!stats.has_index) {
    setStatus("No index loaded");
    els.searchInput.disabled = true;
    return;
  }
  setStatus(
    `${stats.symbol_count} symbols · ${stats.file_count} files · ${stats.provider}`
  );
  els.searchInput.disabled = false;
  els.rootInput.value = stats.root || els.rootInput.value;
  await loadSavedPaths();
}

async function indexProject() {
  const path = els.rootInput.value.trim();
  if (!path) {
    alert("Enter a local project path");
    return;
  }
  els.indexBtn.disabled = true;
  els.indexBtn.textContent = "Indexing…";
  setStatus("Indexing locally…");
  try {
    const result = await api("/api/index", {
      method: "POST",
      body: JSON.stringify({
        path,
        provider: els.providerSelect.value,
      }),
    });
    els.indexHint.textContent = `Index stored at ${result.db}`;
    state.history = [];
    state.historyIndex = -1;
    state.current = null;
    updateNavButtons();
    renderBreadcrumbs();
    els.symbolView.classList.add("hidden");
    els.emptyState.classList.remove("hidden");
    await refreshStats();
  } catch (err) {
    alert(err.message);
    setStatus("Index failed");
  } finally {
    els.indexBtn.disabled = false;
    els.indexBtn.textContent = "Index & load";
  }
}

let searchTimer = null;
function onSearchInput() {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(runSearch, 180);
}

async function runSearch() {
  const q = els.searchInput.value.trim();
  els.searchResults.innerHTML = "";
  if (!q) return;
  try {
    const data = await api(`/api/search?q=${encodeURIComponent(q)}`);
    data.results.forEach((item) => {
      const li = document.createElement("li");
      const btn = document.createElement("button");
      btn.type = "button";
      btn.innerHTML = `<div class="result-title">${escapeHtml(item.name)}</div>
        <div class="result-meta">${escapeHtml(item.kind)} · ${escapeHtml(item.qualname)}</div>`;
      btn.addEventListener("click", () => openSymbol(item.id, true));
      li.appendChild(btn);
      els.searchResults.appendChild(li);
    });
  } catch (err) {
    els.searchResults.innerHTML = `<li class="result-meta">${escapeHtml(err.message)}</li>`;
  }
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

async function openSymbol(symbolId, recordHistory = true) {
  const data = await api(`/api/symbol/${symbolId}`);
  const symbol = data.symbol;
  state.current = symbol;
  if (recordHistory) pushHistory(symbol);

  els.emptyState.classList.add("hidden");
  els.symbolView.classList.remove("hidden");
  els.symbolView.innerHTML = "";

  const card = document.createElement("article");
  card.className = "symbol-card";
  card.innerHTML = `
    <div class="symbol-kind">${escapeHtml(symbol.kind)}</div>
    <h1>${escapeHtml(symbol.name)}</h1>
    <div class="symbol-qual">${escapeHtml(symbol.qualname)}</div>
    <div class="symbol-loc">${escapeHtml(symbol.location.path)}:${symbol.location.line}</div>
    ${symbol.docstring ? `<div class="symbol-doc">${escapeHtml(symbol.docstring)}</div>` : ""}
  `;

  const branches = document.createElement("div");
  branches.className = "branches";

  for (const branch of BRANCHES) {
    const existing = (data.relations || []).filter((r) => r.kind === branch.kind);
    branches.appendChild(buildBranch(symbol.id, branch, existing, data.expanded[branch.kind]));
  }

  card.appendChild(branches);
  els.symbolView.appendChild(card);
  await loadSource(symbol.id);
}

function buildBranch(symbolId, branch, existingRelations, alreadyExpanded) {
  const wrap = document.createElement("div");
  wrap.className = "branch";

  const head = document.createElement("div");
  head.className = "branch-head";
  head.innerHTML = `<h3>${escapeHtml(branch.label)}</h3>`;

  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "branch-toggle";
  toggle.textContent = "Expand";
  head.appendChild(toggle);

  const body = document.createElement("div");
  body.className = "branch-body";

  const renderEdges = (relations) => {
    body.innerHTML = "";
    if (!relations.length) {
      body.innerHTML = `<div class="result-meta" style="padding:0.55rem 0.65rem">No results</div>`;
      return;
    }
    relations.forEach((rel) => {
      const target = rel.to_symbol;
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "edge-btn";
      const label = target ? target.name : rel.to_id;
      const meta = target
        ? `${target.kind} · ${target.location.path}:${target.location.line}`
        : rel.location
          ? `${rel.location.path}:${rel.location.line}`
          : "";
      btn.innerHTML = `<div>
          <div class="edge-name">${escapeHtml(label)}</div>
          <div class="edge-meta">${escapeHtml(meta)}</div>
        </div><div class="edge-meta">open</div>`;
      if (target) {
        btn.addEventListener("click", () => openSymbol(target.id, true));
      } else {
        btn.disabled = true;
      }
      body.appendChild(btn);
    });
  };

  if (alreadyExpanded && existingRelations.length) {
    // Structural relations may already be present; still need target enrichment via expand.
  }

  toggle.addEventListener("click", async () => {
    const opening = !wrap.classList.contains("open");
    if (!opening) {
      wrap.classList.remove("open");
      toggle.textContent = "Expand";
      return;
    }
    toggle.textContent = "Loading…";
    toggle.disabled = true;
    try {
      const data = await api("/api/expand", {
        method: "POST",
        body: JSON.stringify({ symbol_id: symbolId, kind: branch.kind }),
      });
      renderEdges(data.relations || []);
      wrap.classList.add("open");
      toggle.textContent = "Collapse";
    } catch (err) {
      body.innerHTML = `<div class="result-meta" style="padding:0.55rem 0.65rem">${escapeHtml(err.message)}</div>`;
      wrap.classList.add("open");
      toggle.textContent = "Collapse";
    } finally {
      toggle.disabled = false;
    }
  });

  wrap.appendChild(head);
  wrap.appendChild(body);
  return wrap;
}

async function loadSource(symbolId) {
  const snippet = await api(`/api/source/${symbolId}`);
  els.sourcePath.textContent = `${snippet.path}:${snippet.start_line}-${snippet.end_line}`;
  const lines = (snippet.text || "").split("\n");
  els.sourceCode.innerHTML = "";
  lines.forEach((line, idx) => {
    const lineNo = snippet.start_line + idx;
    const row = document.createElement("span");
    row.className = "line" + (lineNo === snippet.highlight_line ? " highlight" : "");
    row.innerHTML = `<span class="ln">${lineNo}</span>${escapeHtml(line)}`;
    els.sourceCode.appendChild(row);
  });
}

async function loadSavedPaths() {
  const data = await api("/api/saved-paths");
  els.savedPaths.innerHTML = "";
  (data.paths || []).forEach((path) => {
    const li = document.createElement("li");
    const btn = document.createElement("button");
    btn.type = "button";
    btn.innerHTML = `<div class="result-title">${escapeHtml(path.name)}</div>
      <div class="result-meta">${path.steps.length} steps · ${escapeHtml(path.created_at)}</div>`;
    btn.addEventListener("click", async () => {
      state.history = path.steps;
      state.historyIndex = path.steps.length - 1;
      updateNavButtons();
      renderBreadcrumbs();
      if (path.steps.length) {
        state.suppressHistory = true;
        await openSymbol(path.steps[path.steps.length - 1].id, false);
        state.suppressHistory = false;
      }
    });
    li.appendChild(btn);
    els.savedPaths.appendChild(li);
  });
}

async function saveCurrentPath() {
  if (!state.history.length) return;
  const name = prompt("Name this exploration path", state.history.map((s) => s.name).join(" → "));
  if (!name) return;
  await api("/api/saved-paths", {
    method: "POST",
    body: JSON.stringify({ name, steps: state.history.slice(0, state.historyIndex + 1) }),
  });
  await loadSavedPaths();
}

async function init() {
  try {
    const health = await api("/api/health");
    els.providerSelect.innerHTML = "";
    (health.providers || []).forEach((p) => {
      const opt = document.createElement("option");
      opt.value = p.name;
      opt.textContent = `${p.name} (${(p.languages || []).join(", ")})`;
      els.providerSelect.appendChild(opt);
    });
    await refreshStats();
  } catch (err) {
    setStatus("Server unavailable");
  }

  els.indexBtn.addEventListener("click", indexProject);
  els.searchInput.addEventListener("input", onSearchInput);
  els.backBtn.addEventListener("click", () => jumpToHistory(state.historyIndex - 1));
  els.forwardBtn.addEventListener("click", () => jumpToHistory(state.historyIndex + 1));
  els.savePathBtn.addEventListener("click", saveCurrentPath);
}

init();
