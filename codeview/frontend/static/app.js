const SECTIONS = [
  { kind: "called_by", label: "Called by" },
  { kind: "calls", label: "Calls" },
  { kind: "parent_class", label: "Inheritance" },
  { kind: "implemented_by", label: "Implementations" },
  { kind: "child_class", label: "Subtypes" },
  { kind: "contains", label: "Members" },
  { kind: "referenced_by", label: "References" },
];

const state = {
  history: [],
  historyIndex: -1,
  selectedId: null,
  openSections: new Set(),
};

const els = {
  status: document.getElementById("status"),
  filter: document.getElementById("filter"),
  results: document.getElementById("results"),
  listMeta: document.getElementById("listMeta"),
  explore: document.getElementById("explore"),
  exploreEmpty: document.getElementById("exploreEmpty"),
  symName: document.getElementById("symName"),
  symMeta: document.getElementById("symMeta"),
  src: document.getElementById("src"),
  srcMeta: document.getElementById("srcMeta"),
  sections: document.getElementById("sections"),
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

function setUrl(symbolId) {
  const next = symbolId ? `#/s/${encodeURIComponent(symbolId)}` : "#/";
  if (location.hash !== next) history.pushState(null, "", next);
}

function readUrlSymbol() {
  const m = location.hash.match(/^#\/s\/(.+)$/);
  return m ? decodeURIComponent(m[1]) : null;
}

function updateNav() {
  els.back.disabled = state.historyIndex <= 0;
  els.forward.disabled = state.historyIndex >= state.history.length - 1;
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

function renderResults(symbols, meta) {
  els.listMeta.textContent = meta || "";
  els.results.innerHTML = "";
  if (!symbols.length) {
    els.results.innerHTML = `<li class="empty">No matches</li>`;
    return;
  }
  symbols.forEach((symbol) => {
    const li = document.createElement("li");
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "result" + (state.selectedId === symbol.id ? " active" : "");
    btn.innerHTML = `<span class="name">${esc(symbol.name)}</span><span class="kind">${esc(symbol.kind)}</span><span class="loc">${esc(symbol.location.path)}${symbol.kind === "directory" ? "" : ":" + symbol.location.line}</span>`;
    btn.addEventListener("click", () => openSymbol(symbol, true));
    li.appendChild(btn);
    els.results.appendChild(li);
  });
}

async function loadSearch(q) {
  const data = await api(`/api/tree?q=${encodeURIComponent(q)}&limit=80`);
  renderResults(data.results || [], q ? `search: ${q}` : "top-level symbols");
}

async function openSymbol(symbol, record) {
  if (!symbol || symbol.kind === "directory") return;
  state.selectedId = symbol.id;
  state.openSections = new Set(["called_by", "calls"]);
  if (record) {
    pushHistory(symbol);
    setUrl(symbol.id);
  }
  document.querySelectorAll(".result").forEach((el) => el.classList.remove("active"));

  els.exploreEmpty.hidden = true;
  els.explore.hidden = false;
  els.symName.textContent = symbol.qualname && symbol.qualname.includes("::")
    ? symbol.name
    : symbol.name;
  els.symMeta.textContent = `${symbol.kind} · ${symbol.location.path}:${symbol.location.line}` +
    (symbol.language ? ` · ${symbol.language}` : "");

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

  els.sections.innerHTML = "";
  for (const section of SECTIONS) {
    els.sections.appendChild(makeSection(symbol, section));
  }
}

function makeSection(symbol, section) {
  const wrap = document.createElement("div");
  wrap.className = "section";
  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "section-toggle";
  const open = state.openSections.has(section.kind);
  toggle.innerHTML = `<span class="arrow">${open ? "▼" : "▶"}</span><span class="label">${esc(section.label)}</span><span class="count"></span>`;
  const body = document.createElement("div");
  body.className = "section-body";
  body.hidden = !open;
  toggle.addEventListener("click", async () => {
    if (body.hidden) {
      state.openSections.add(section.kind);
      body.hidden = false;
      toggle.querySelector(".arrow").textContent = "▼";
      await fillSection(symbol, section, body, toggle);
    } else {
      state.openSections.delete(section.kind);
      body.hidden = true;
      toggle.querySelector(".arrow").textContent = "▶";
    }
  });
  wrap.appendChild(toggle);
  wrap.appendChild(body);
  if (open) fillSection(symbol, section, body, toggle);
  return wrap;
}

async function fillSection(symbol, section, body, toggle) {
  body.innerHTML = `<div class="empty">loading…</div>`;
  try {
    const data = await api("/api/expand", {
      method: "POST",
      body: JSON.stringify({ symbol_id: symbol.id, kind: section.kind }),
    });
    const items = (data.relations || [])
      .map((r) => r.to_symbol)
      .filter(Boolean);
    const seen = new Set();
    const unique = [];
    for (const s of items) {
      if (seen.has(s.id)) continue;
      seen.add(s.id);
      unique.push(s);
    }
    const total = data.total ?? unique.length;
    toggle.querySelector(".count").textContent = total
      ? data.truncated
        ? `(${unique.length} of ${total})`
        : `(${total})`
      : "";
    body.innerHTML = "";
    if (!unique.length) {
      body.innerHTML = `<div class="empty">none</div>`;
      return;
    }
    unique.forEach((s) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "edge";
      btn.innerHTML = `<span>${esc(s.name)}</span><span class="kind">${esc(s.kind)}</span><span class="loc">${esc(s.location.path)}:${s.location.line}</span>`;
      btn.addEventListener("click", () => openSymbol(s, true));
      body.appendChild(btn);
    });
  } catch (err) {
    body.innerHTML = `<div class="empty">${esc(err.message)}</div>`;
  }
}

async function openById(id, record) {
  const data = await api(`/api/symbol/${id}`);
  await openSymbol(data.symbol, record);
}

async function init() {
  try {
    const stats = await api("/api/stats");
    if (!stats.has_index) {
      els.status.textContent = "no index — run: codeview serve .";
      return;
    }
    const providers = stats.provider || "";
    els.status.textContent = `${stats.symbol_count || stats.file_count || "?"} symbols · ${providers} · ${stats.root || ""}`;
    const fromUrl = readUrlSymbol();
    if (fromUrl) {
      await openById(fromUrl, true);
      await loadSearch(fromUrl.slice(0, 12));
    } else {
      await loadSearch("");
    }
  } catch (err) {
    els.status.textContent = err.message;
  }

  let t = null;
  els.filter.addEventListener("input", () => {
    clearTimeout(t);
    t = setTimeout(() => loadSearch(els.filter.value.trim()), 150);
  });
  els.back.addEventListener("click", async () => {
    if (state.historyIndex <= 0) return;
    state.historyIndex -= 1;
    updateNav();
    await openById(state.history[state.historyIndex].id, false);
    setUrl(state.history[state.historyIndex].id);
  });
  els.forward.addEventListener("click", async () => {
    if (state.historyIndex >= state.history.length - 1) return;
    state.historyIndex += 1;
    updateNav();
    await openById(state.history[state.historyIndex].id, false);
    setUrl(state.history[state.historyIndex].id);
  });
  window.addEventListener("hashchange", async () => {
    const id = readUrlSymbol();
    if (id && id !== state.selectedId) await openById(id, true);
  });
}

init();
