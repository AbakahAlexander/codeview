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
  srcMeta: document.getElementById("srcMeta"),
  srcCallBlock: document.getElementById("srcCallBlock"),
  srcCallLabel: document.getElementById("srcCallLabel"),
  srcCall: document.getElementById("srcCall"),
  srcDefBlock: document.getElementById("srcDefBlock"),
  srcDefLabel: document.getElementById("srcDefLabel"),
  srcDef: document.getElementById("srcDef"),
  crumbs: document.getElementById("crumbs"),
  back: document.getElementById("back"),
  forward: document.getElementById("forward"),
};

function renderSnippet(pre, snippet) {
  const lines = (snippet.text || "").split("\n");
  pre.innerHTML = lines
    .map((line, i) => {
      const n = snippet.start_line + i;
      const hl = n === snippet.highlight_line ? " hl" : "";
      return `<span class="line${hl}"><span class="ln">${n}</span>${esc(line)}</span>`;
    })
    .join("");
  const hlEl = pre.querySelector(".line.hl");
  if (hlEl) hlEl.scrollIntoView({ block: "center", behavior: "smooth" });
}

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
    call_site: !!symbol.call_site,
    edge: symbol.edge || null,
    peer_id: symbol.peer_id || null,
    peer_name: symbol.peer_name || null,
    def_location: symbol.def_location || null,
    signature: symbol.signature || null,
  };
  state.history = state.history.slice(0, state.historyIndex + 1);
  const cur = state.history[state.historyIndex];
  if (
    cur &&
    cur.id === step.id &&
    cur.line === step.line &&
    cur.path === step.path &&
    cur.call_site === step.call_site &&
    cur.peer_id === step.peer_id
  ) {
    updateNav();
    return;
  }
  state.history.push(step);
  state.historyIndex = state.history.length - 1;
  updateNav();
}

function snippetContains(snippet, path, line) {
  if (!snippet || !path || !line) return false;
  return (
    snippet.path === path &&
    line >= snippet.start_line &&
    line <= snippet.end_line
  );
}

async function selectSymbol(symbol, record = true) {
  state.selectedId = symbol.id;
  document.querySelectorAll(".sym.active").forEach((el) => el.classList.remove("active"));
  document.querySelectorAll(`.sym[data-id="${symbol.id}"]`).forEach((el) => el.classList.add("active"));
  if (record) pushHistory(symbol);
  if (symbol.kind === "directory") {
    els.srcMeta.textContent = symbol.location.path;
    els.srcCallBlock.hidden = true;
    els.srcDefLabel.textContent = "directory";
    els.srcDef.textContent = "(directory — expand with + to browse)";
    return;
  }

  // Call edges: full caller body (highlight call line) + callee definition.
  // edge "calls": clicked = callee, peer = caller
  // edge "called_by": clicked = caller, peer = callee
  const defLoc = symbol.def_location || null;
  const loc = symbol.location || {};
  if (
    loc.line &&
    symbol.call_site &&
    symbol.peer_id &&
    (symbol.edge === "calls" || symbol.edge === "called_by")
  ) {
    const callLine = loc.line;
    const callPath = loc.path || "";
    const callerId = symbol.edge === "called_by" ? symbol.id : symbol.peer_id;
    const calleeId = symbol.edge === "called_by" ? symbol.peer_id : symbol.id;
    const calleeName =
      symbol.edge === "calls" ? symbol.name : symbol.peer_name || "symbol";
    try {
      const callQs = new URLSearchParams({
        line: String(callLine),
        path: callPath,
        span: "body",
      });
      const callSnippet = await api(`/api/source/${callerId}?${callQs}`);
      const callFocus = callSnippet.highlight_line || callLine;
      els.srcCallBlock.hidden = false;
      els.srcCallLabel.textContent = `call site · ${callSnippet.path}:${callFocus}`;
      renderSnippet(els.srcCall, callSnippet);

      let calleeMeta = null;
      try {
        calleeMeta = (await api(`/api/symbol/${calleeId}`)).symbol;
      } catch (_) {
        calleeMeta = null;
      }
      const calleeSig =
        (symbol.edge === "calls" ? symbol.signature : null) ||
        (calleeMeta && calleeMeta.signature) ||
        null;
      const bindLoc =
        (symbol.edge === "calls" && defLoc) ||
        (calleeMeta && calleeMeta.location) ||
        null;
      const bindDiffers =
        !!(
          bindLoc &&
          bindLoc.line &&
          (bindLoc.path !== callPath || bindLoc.line !== callLine)
        );
      const unresolved = calleeSig === "unresolved" || calleeSig === "external";

      if (unresolved) {
        await renderMissingDefinition(calleeName, calleeSig, {
          callPath,
          callFocus,
          // Only show a binding line when it is not the call site itself.
          bindLoc: bindDiffers ? bindLoc : null,
          calleeId,
        });
        return;
      }

      const defSnippet = await api(`/api/source/${calleeId}?span=body`);
      const defFocus = defSnippet.highlight_line || bindLoc.line || defSnippet.start_line;
      // Guard: never show the call site again as if it were the definition.
      if (
        defSnippet.path === callSnippet.path &&
        (defSnippet.highlight_line || defSnippet.start_line) === callFocus
      ) {
        await renderMissingDefinition(calleeName, calleeSig || "unresolved", {
          callPath,
          callFocus,
          bindLoc: null,
          calleeId,
        });
        return;
      }
      els.srcDefLabel.textContent = `definition · ${defSnippet.path}:${defFocus}`;
      renderSnippet(els.srcDef, defSnippet);
      els.srcMeta.textContent = `${callSnippet.path}:${callFocus} ↔ ${defSnippet.path}:${defFocus}`;
      return;
    } catch (_) {
      // fall through to single-pane definition
    }
  }

  // Prefer the real definition location when the row carries a call-site path.
  const defQs =
    defLoc && defLoc.line
      ? `?line=${encodeURIComponent(defLoc.line)}&path=${encodeURIComponent(defLoc.path || "")}`
      : "";
  const snippet = await api(`/api/source/${symbol.id}${defQs}`);
  const focus = snippet.highlight_line || (defLoc && defLoc.line) || loc.line;
  const sig = symbol.signature;
  if (sig === "unresolved" || sig === "external") {
    els.srcCallBlock.hidden = true;
    await renderMissingDefinition(symbol.name, sig, {
      callPath: loc.path || "",
      callFocus: focus,
      bindLoc: defLoc && defLoc.line && (defLoc.path !== loc.path || defLoc.line !== loc.line) ? defLoc : null,
      calleeId: symbol.id,
    });
    return;
  }
  els.srcMeta.textContent = `${snippet.path}:${focus} · ${snippet.start_line}-${snippet.end_line}`;
  els.srcCallBlock.hidden = true;
  els.srcDefLabel.textContent = `definition · ${snippet.path}:${focus}`;
  renderSnippet(els.srcDef, snippet);
}

async function renderMissingDefinition(name, signature, { callPath, callFocus, bindLoc, calleeId }) {
  const kind =
    signature === "external"
      ? "external (dependency / stdlib)"
      : signature === "unresolved"
        ? "unresolved"
        : "not found in this repository";
  els.srcDefLabel.textContent = `definition · ${kind}`;
  els.srcMeta.textContent = `${callPath}:${callFocus} ↔ (no local definition)`;

  let bindText = "";
  if (bindLoc && bindLoc.line && calleeId) {
    try {
      const bindQs = new URLSearchParams({
        line: String(bindLoc.line),
        path: bindLoc.path || "",
        context: "0",
      });
      const bindSnippet = await api(`/api/source/${calleeId}?${bindQs}`);
      const line = (bindSnippet.text || "").trim();
      if (line) {
        bindText =
          `Local binding / import (${bindLoc.path}:${bindLoc.line}):\n` +
          `  ${line}\n\n`;
      }
    } catch (_) {
      /* ignore */
    }
  }

  els.srcDef.textContent =
    bindText +
    `${name} has no definition body in this repository.\n` +
    `It is ${kind}. Only the call site is available locally.`;
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

  // File modules (``__main__.py``, etc.): callers/callees only — "symbols"/"uses"
  // duplicate the same call/import targets and bury the call graph.
  const kinds =
    symbol.kind === "module" && symbol.signature === "file"
      ? [
          { kind: "called_by", label: "callers" },
          { kind: "calls", label: "callees" },
        ]
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
              call_site: true,
              edge: branch.kind,
              peer_id: symbol.id,
              peer_name: symbol.name,
              def_location: s.location,
              location: {
                path: site.path || s.location.path,
                line: site.line,
                column: site.column ?? 0,
                end_line: s.location.end_line ?? null,
                end_column: s.location.end_column ?? null,
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
    els.status.textContent = `${pct}%`;
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
    if (phase === "error") {
      const err = st.error || st.message || "index failed";
      els.tree.innerHTML = `<li class="empty">${esc(err)}</li>`;
      return;
    }
    // While indexing, show whatever graph already exists.
    if (st.has_graph) {
      await loadTree();
    } else {
      els.tree.innerHTML = `<li class="empty">${st.percent || 0}%</li>`;
    }
    await new Promise((r) => setTimeout(r, 350));
  }
}

async function init() {
  try {
    const stats = await api("/api/stats");
    const st = stats.index_status || (await api("/api/index-status"));
    if (st && st.status === "error") {
      const err = st.error || st.message || "index failed";
      els.status.textContent = err;
      els.tree.innerHTML = `<li class="empty">${esc(err)}</li>`;
      return;
    }
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
  async function restoreStep(step) {
    const data = await api(`/api/symbol/${step.id}`);
    const symbol = step.call_site
      ? {
          ...data.symbol,
          call_site: true,
          edge: step.edge,
          peer_id: step.peer_id,
          peer_name: step.peer_name,
          def_location: step.def_location || data.symbol.location,
          signature: step.signature || data.symbol.signature,
          location: {
            ...data.symbol.location,
            path: step.path,
            line: step.line,
          },
        }
      : data.symbol;
    await selectSymbol(symbol, false);
  }

  els.back.addEventListener("click", async () => {
    if (state.historyIndex <= 0) return;
    state.historyIndex -= 1;
    updateNav();
    await restoreStep(state.history[state.historyIndex]);
  });
  els.forward.addEventListener("click", async () => {
    if (state.historyIndex >= state.history.length - 1) return;
    state.historyIndex += 1;
    updateNav();
    await restoreStep(state.history[state.historyIndex]);
  });
}

init();
