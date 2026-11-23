// DAKC local UI: a read-only view of one workspace, served by `dakc serve`.
// No framework, no build step, no network access beyond this server.

const $ = (sel, el = document) => el.querySelector(sel);
const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ESC[c]);
const fmt = (n) => Number(n || 0).toLocaleString("en-US");
const enc = encodeURIComponent;
const plural = (n, one, many) => `${fmt(n)} ${n === 1 ? one : many}`;
const sum = (xs) => xs.reduce((a, b) => a + (b || 0), 0);

// Top (index) to bottom (sources): knowledge is built upwards from the source documents.
const LAYERS = [
  { kind: "index", label: "Index" },
  { kind: "summary", label: "Summaries" },
  { kind: "page", label: "Pages" },
  { kind: "entity", label: "Entities" },
  { kind: "claim", label: "Claims" },
  { kind: "passage", label: "Passages" },
  { kind: "source", label: "Sources" },
];
const KIND = { source: "Source", passage: "Passage", claim: "Claim", entity: "Entity", relationship: "Relationship",
  page: "Page", summary: "Summary", index: "Index" };
const LIGHT = new Set(["entity", "page", "summary", "index"]);
const swatch = (kind) => `<span class="swatch" style="background:var(--l-${esc(kind)})"></span>`;
const link = (id, text) => `<a href="#/a/${enc(id)}">${esc(text ?? id)}</a>`;

async function api(path) {
  const r = await fetch(path, { headers: { Accept: "application/json" } });
  if (!r.ok) {
    let msg = `${r.status} ${r.statusText}`;
    try { msg = (await r.json()).detail || msg; } catch (e) { /* not JSON */ }
    throw new Error(msg);
  }
  return r.json();
}

// ------------------------------------------------------------------ rendering guard
let seq = 0;
function begin(view, html = '<p class="loading">Loading…</p>') {
  const token = ++seq;
  view.innerHTML = html;
  return () => token === seq; // still current?
}

// ------------------------------------------------------------------ change overlays
function overlay(report, mode) {
  const oc = report.operation_counts || {};
  const sc = report.scan || {};
  const g = (k) => oc[k] || 0;
  return {
    mode,
    layers: {
      source: { changed: sum([sc.new, sc.modified, sc.deleted, sc.renamed]),
        note: (n) => `<b>${fmt(n)}</b> changed` },
      passage: { changed: g("reextract"), note: (n) => `<b>${fmt(n)}</b> re-extracted` },
      claim: { changed: g("new_claim") + g("retract_claim"),
        note: () => `<b>${fmt(g("new_claim"))}</b> new · <b>${fmt(g("retract_claim"))}</b> retracted` },
      entity: { changed: g("reconcile_entity") + g("delete_entity"), note: (n) => `<b>${fmt(n)}</b> reconciled` },
      page: { changed: g("regenerate_page") + g("rerender_page") + g("delete_page"), checked: g("cutoff_page"),
        note: () => `<b>${fmt(g("regenerate_page"))}</b> regenerated` +
          (g("rerender_page") ? ` · <b>${fmt(g("rerender_page"))}</b> re-rendered` : "") +
          (g("cutoff_page") ? ` · ${fmt(g("cutoff_page"))} checked, unchanged` : "") },
      summary: { changed: g("regenerate_summary") + g("rerender_summary") + g("delete_summary"), checked: g("cutoff_summary"),
        note: () => `<b>${fmt(g("regenerate_summary") + g("rerender_summary"))}</b> updated` +
          (g("cutoff_summary") ? ` · ${fmt(g("cutoff_summary"))} checked, unchanged` : "") },
      index: { changed: g("rerender_index"), checked: g("cutoff_index"),
        note: () => (g("rerender_index") ? "<b>re-rendered</b>" : g("cutoff_index") ? "checked, unchanged" : "") },
    },
  };
}

function llmCalls(report) {
  return (report.usage?.llm_calls || 0) + sum(Object.values(report.planned_llm_calls || {}));
}

// The signature: the knowledge base as a cross-section, sources at the bottom.
function stack(counts, ov) {
  const max = Math.max(1, ...LAYERS.map((l) => counts[l.kind] || 0));
  const rows = LAYERS.map((l) => {
    const n = counts[l.kind] || 0;
    const w = n ? 16 + 84 * (Math.log10(n + 1) / Math.log10(max + 1)) : 0;
    const o = ov?.layers[l.kind];
    let slices = "", note = `<span class="quiet">${fmt(n)}</span>`;
    if (o) {
      const changed = Math.min(o.changed || 0, n || o.changed || 0);
      const cw = changed ? `max(4px, calc(${w}% * ${Math.min(1, changed / Math.max(n, 1))}))` : "0px";
      const kw = o.checked ? `max(6px, calc(${w}% * ${Math.min(1, o.checked / Math.max(n, 1))}))` : "0px";
      if (changed) slices += `<span class="slice-changed" style="left:calc(${w}% - ${cw});width:${cw}"></span>`;
      if (o.checked) slices += `<span class="slice-checked" style="left:calc(${w}% - ${cw} - ${kw});width:${kw}"></span>`;
      const untouched = Math.max(0, n - (o.changed || 0) - (o.checked || 0));
      const body = o.changed || o.checked ? o.note(o.changed) : "";
      note = body ? `${body}<br><span class="quiet">${fmt(untouched)} untouched</span>` : `<span class="quiet">${fmt(n)} untouched</span>`;
    }
    return `<div class="stratum" ${LIGHT.has(l.kind) ? "data-light" : ""} style="--layer:var(--l-${l.kind})">
      <div class="stratum-name"><a href="#/explore?kind=${l.kind}">${l.label}</a></div>
      <div class="stratum-track">
        <div class="stratum-bar" style="width:${w}%"></div>${slices}<span class="stratum-count num">${fmt(n)}</span>
      </div>
      <div class="stratum-note">${note}</div>
    </div>`;
  }).join("");
  const words = { pending: "will be rebuilt", last: "rebuilt by the last build", impact: "would be invalidated" };
  const anyChecked = ov && Object.values(ov.layers).some((o) => o.checked);
  const legend = ov
    ? `<div class="legend"><span><i class="${ov.mode === "last" ? "k-last" : "k-changed"}"></i>${words[ov.mode]}</span>${anyChecked ? '<span><i class="k-checked"></i>checked, found unchanged (the rebuild stops there)</span>' : ""}</div>`
    : "";
  return `<div class="stack" data-mode="${ov?.mode || "none"}" role="img" aria-label="Artifacts per layer${ov ? " and what changes" : ""}">${rows}</div>${legend}`;
}

// ------------------------------------------------------------------ markdown (DAKC's own pages)
function inline(text, ctx) {
  const codes = [];
  let s = esc(text).replace(/`([^`]+)`/g, (_, c) => `\u0000${codes.push(c) - 1}\u0000`);
  s = s.replace(/\[\^(C\d+)\]/g, (_, ref) => `<button type="button" class="cite" data-ref="${ref}" aria-pressed="false" aria-label="Show where ${ref} comes from">${ref}</button>`);
  s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_, label, href) => {
    const raw = href.replace(/&amp;/g, "&");
    if (/^https?:\/\//.test(raw)) return `<a href="${href}" target="_blank" rel="noopener">${label}</a>`;
    const id = wikiTarget(raw, ctx.kind);
    return id ? `<a href="#/a/${enc(id)}">${label}</a>` : label;
  });
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  return s.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${codes[+i]}</code>`);
}

function wikiTarget(href, kind) {
  const base = href.split("#")[0];
  let m = base.match(/(?:^|\/)(pages|summaries)\/([^/]+)\.md$/);
  if (m) return (m[1] === "pages" ? "page:" : "sum:") + m[2];
  if (/(?:^|\/)index\.md$/.test(base)) return "index:main";
  m = base.match(/^([^/]+)\.md$/);
  if (m) return (kind === "summary" ? "sum:" : "page:") + m[1];
  return null;
}

function markdown(md, ctx) {
  const out = [], notes = [];
  let para = [], list = false;
  const flush = () => { if (para.length) { out.push(`<p>${inline(para.join(" "), ctx)}</p>`); para = []; } };
  const close = () => { if (list) { out.push("</ul>"); list = false; } };
  for (const line of md.split("\n")) {
    let m;
    if ((m = line.match(/^\[\^(C\d+)\]:\s*(.*)$/))) { notes.push([m[1], m[2]]); continue; }
    if ((m = line.match(/^(#{1,6})\s+(.*)$/))) {
      flush(); close();
      if (/^sources$/i.test(m[2].trim()) && m[1].length === 2) continue; // rendered below
      out.push(`<h${m[1].length}>${inline(m[2], ctx)}</h${m[1].length}>`);
    } else if ((m = line.match(/^\s*[-*]\s+(.*)$/))) {
      flush(); if (!list) { out.push("<ul>"); list = true; }
      out.push(`<li>${inline(m[1], ctx)}</li>`);
    } else if (!line.trim()) { flush(); close(); }
    else { close(); para.push(line.trim()); }
  }
  flush(); close();
  if (notes.length) {
    const docs = new Set(notes.flatMap(([, paths]) => paths.split(", ")));
    out.push(`<details class="footnotes"><summary>Sources · ${plural(notes.length, "citation", "citations")} from ${plural(docs.size, "document", "documents")}</summary><ol>${notes.map(([ref, paths]) =>
      `<li><button type="button" class="cite" data-ref="${ref}" aria-pressed="false" aria-label="Show where ${ref} comes from">${ref}</button> ${paths.split(", ").map((p) => link(p, p)).join(", ")}</li>`).join("")}</ol></details>`);
  }
  return out.join("\n");
}

// Headings may carry attribute lists such as "Form Data { #form-data }" (MkDocs anchors).
const heading = (h) => h.replace(/\s*\{[^}]*#[^}]*\}/g, "");

function highlight(text, sentences) {
  const spans = [];
  for (const s of sentences || []) {
    const i = s ? text.indexOf(s) : -1;
    if (i >= 0) spans.push([i, i + s.length]);
  }
  spans.sort((a, b) => a[0] - b[0]);
  let out = "", at = 0;
  for (const [a, b] of spans) {
    if (a < at) continue;
    out += esc(text.slice(at, a)) + `<mark>${esc(text.slice(a, b))}</mark>`;
    at = b;
  }
  return out + esc(text.slice(at));
}

// ------------------------------------------------------------------ views
async function overview(view) {
  const live = begin(view);
  const [st, recent] = await Promise.all([api("/status"), api("/builds?limit=6")]);
  const plan = st.up_to_date ? null : await api("/plan");
  if (!live()) return;
  const last = recent.find((b) => b.status === "committed");
  const ov = plan ? overlay(plan, "pending") : last ? overlay(last.stats || {}, "last") : null;
  const changedSources = sum([st.source_changes.new, st.source_changes.modified, st.source_changes.deleted, st.source_changes.renamed]);
  let head, lede;
  if (plan) {
    const oc = plan.operation_counts || {};
    head = changedSources ? `${plural(changedSources, "source changed", "sources changed")} since build ${st.head}.`
      : `Sources unchanged since build ${st.head}; DAKC or its settings changed.`;
    lede = (st.outdated?.length ? `${esc(st.outdated.join("; "))}. ` : "") + `A compile would redo <strong>${plural(llmCalls(plan), "LLM call", "LLM calls")}</strong> and regenerate
      <strong>${fmt(oc.regenerate_page)} of ${fmt(st.counts.page)} pages</strong>. Everything else stays exactly as it is.
      <a href="#/changes">See the plan</a>.`;
  } else {
    head = `Up to date with its ${plural(st.counts.source || 0, "source", "sources")}.`;
    const oc = last?.stats?.operation_counts || {};
    lede = last ? `Build ${last.id} regenerated <strong>${fmt(oc.regenerate_page)} of ${fmt(st.counts.page)} pages</strong>
      with ${plural(last.stats?.usage?.llm_calls || 0, "LLM call", "LLM calls")}.` : "Nothing has been compiled yet.";
  }
  view.innerHTML = `
    <p class="eyebrow">${swatch("source")} Knowledge base · build ${st.head || "–"}</p>
    <h1 class="title">${esc(head)}</h1>
    <p class="lede">${lede}</p>
    ${st.conflicts_open ? `<div class="callout conflict"><a href="#/conflicts"><strong>${plural(st.conflicts_open, "open conflict", "open conflicts")}</strong></a>
      between documents: statements that cannot both be true.</div>` : ""}
    ${stack(st.counts, ov)}
    <div class="grid-2" style="margin-top:36px">
      <section class="card"><h3>Recent builds</h3><ul class="list timeline">${recent.map((b) => buildRow(b, st.head)).join("")}</ul>
        <p style="margin:12px 0 0"><a href="#/builds">All builds</a></p></section>
      <section class="card"><h3>Workspace</h3><dl class="kv">
        <dt>Sources</dt><dd class="mono" title="${esc(st.source_root)}">${esc(shortPath(st.source_root))}</dd>
        <dt>Provider</dt><dd>${esc(st.provider)}</dd>
        <dt>Claims</dt><dd class="num">${fmt(st.counts.claim)} from ${fmt(st.counts.passage)} passages</dd>
        <dt>Edges</dt><dd class="num">${fmt(st.counts.edges)} dependencies</dd></dl></section>
    </div>`;
}

function shortPath(p) {
  const parts = p.split("/").filter(Boolean);
  return parts.length > 3 ? `…/${parts.slice(-3).join("/")}` : p;
}

const OPS = {
  source_new: "new sources", source_modified: "modified sources", source_deleted: "deleted sources",
  source_renamed: "renamed sources", source_processed: "sources read", reextract: "passages re-extracted",
  extraction_cache_hit: "extraction cache hits", sentence_reuse_only: "passages reused sentence by sentence",
  extractor_changed: "re-extracted for a new extractor", new_claim: "new claims", retract_claim: "claims retracted",
  claim_provenance_changed: "claims with changed sources", claim_reproduced: "claims reproduced",
  reconcile_entity: "entities reconciled", delete_entity: "entities deleted", regenerate_page: "pages regenerated",
  rerender_page: "pages re-rendered", cutoff_page: "pages checked, unchanged", delete_page: "pages deleted",
  regenerate_summary: "summaries regenerated", rerender_summary: "summaries re-rendered",
  cutoff_summary: "summaries checked, unchanged", delete_summary: "summaries deleted",
  rerender_index: "index re-rendered", cutoff_index: "index checked, unchanged",
  passage_heading_changed: "passages under a changed heading", source_revised: "sources with a new revision, same content", regenerate_section: "sections rewritten",
  cutoff_section: "sections checked, unchanged",
};

function statusPill(status, head, id) {
  const cls = { committed: "valid", failed: "deleted", rolled_back: "stale" }[status] || "";
  return `<span class="pill ${cls}">${esc(status.replace("_", " "))}${id === head ? " · HEAD" : ""}</span>`;
}

function buildRow(b, head, current) {
  const calls = b.stats?.usage?.llm_calls;
  const when = b.finished_at ? new Date(b.finished_at).toLocaleString() : "–";
  return `<li><a class="row" href="#/builds/${b.id}" ${current ? 'aria-current="true"' : ""}>
    <span class="num mono">#${b.id}</span>
    <span class="t">${statusPill(b.status, head, b.id)}<span class="sub">${esc(b.strategy)} · ${esc(when)}</span></span>
    <span class="id num">${calls == null ? "" : plural(calls, "LLM call", "LLM calls")}</span></a></li>`;
}

const EXPLORE_KINDS = [["page", "Pages"], ["claim", "Claims"], ["passage", "Passages"], ["source", "Sources"], ["summary", "Summaries"]];

async function explore(view, params) {
  const kind = params.get("kind") || "page";
  const q = params.get("q") || "";
  const live = begin(view, `
    <p class="eyebrow">${swatch(kind)} Explore</p>
    <h1 class="title">Find anything, then see where it came from.</h1>
    <p class="lede">Pages and summaries match their title. Claims and passages match their full text. Sources match their path.</p>
    <div class="toolbar">
      <input type="search" id="q" value="${esc(q)}" placeholder="e.g. UploadFile, parser limit, dependencies" aria-label="Search">
      <div class="seg" role="group" aria-label="Kind">${EXPLORE_KINDS.map(([k, label]) =>
        `<button type="button" data-kind="${k}" aria-pressed="${k === kind}">${label}<span class="muted" data-count="${k}"></span></button>`).join("")}</div>
    </div>
    <ul class="list card" id="results" style="padding:4px 12px"><li class="loading">Loading…</li></ul>`);
  const input = $("#q", view);
  const go = (k, text) => { history.replaceState(null, "", `#/explore?kind=${k}&q=${enc(text)}`); explore(view, new URLSearchParams(`kind=${k}&q=${enc(text)}`)); };
  view.querySelectorAll("[data-kind]").forEach((b) => b.addEventListener("click", () => go(b.dataset.kind, input.value)));
  let timer;
  input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => go(kind, input.value), 300); });
  if (q) { input.focus(); input.setSelectionRange(q.length, q.length); }
  const limit = 60;
  const all = await Promise.all(EXPLORE_KINDS.map(([k]) => api(`/artifacts?kind=${k}&query=${enc(q)}&limit=${limit}`).catch(() => [])));
  if (!live()) return;
  EXPLORE_KINDS.forEach(([k], i) => { const el = view.querySelector(`[data-count="${k}"]`); if (el && q) el.textContent = ` ${all[i].length >= limit ? `${limit}+` : all[i].length}`; });
  const items = all[EXPLORE_KINDS.findIndex(([k]) => k === kind)];
  $("#results", view).innerHTML = items.length
    ? items.map((a) => `<li><a class="row" href="#/a/${enc(a.id)}">${swatch(a.kind)}<span class="t">${esc(a.title)}</span><span class="id">${esc(a.id)}</span></a></li>`).join("")
    : `<li class="empty" style="border:0">Nothing matches “${esc(q)}” among ${EXPLORE_KINDS.find(([k]) => k === kind)[1].toLowerCase()}. Try another kind above, or a shorter phrase.</li>`;
}

async function artifact(view, ident, params) {
  const tab = params.get("tab") || "read";
  const live = begin(view);
  let exp;
  try { exp = await api(`/artifacts/${enc(ident)}`); }
  catch (e) {
    if (!live()) return;
    view.innerHTML = `<p class="eyebrow">Not found</p><h1 class="title">${esc(ident)}</h1><div class="empty">${esc(e.message)}</div>`;
    return;
  }
  const notice = await conflictNotice(exp);
  if (!live()) return;
  const id = exp.id, kind = exp.kind;
  const statusCls = { VALID: "valid", STALE: "stale", DELETED: "deleted" }[exp.status] || "";
  const tabs = [["read", kind === "source" ? "Document" : "Read"], ["deps", "Dependencies"], ["impact", "Impact"], ["graph", "Graph"]];
  view.innerHTML = `
    <p class="eyebrow">${swatch(kind)} ${KIND[kind] || kind}</p>
    <h1 class="title">${esc(exp.title)}</h1>
    <div class="chips"><span class="pill mono">${esc(id)}</span><span class="pill ${statusCls}">${esc(exp.status.toLowerCase())}</span>
      ${exp.version ? `<span class="pill">version ${exp.version}</span>` : ""}
      ${exp.last_changed_build ? `<span class="pill">built in #${exp.last_changed_build}</span>` : ""}</div>
    ${exp.reasons?.length ? `<div class="callout">${exp.reasons.map(esc).join("<br>")}. <a href="#/changes">What a compile would redo</a>.</div>` : ""}
    ${notice}
    <nav class="tabs" aria-label="Artifact views">${tabs.map(([t, label]) =>
      `<a href="#/a/${enc(id)}?tab=${t}" ${t === tab ? 'aria-current="page"' : ""}>${label}</a>`).join("")}</nav>
    <div id="tab"><p class="loading">Loading…</p></div>`;
  const pane = $("#tab", view);
  const render = { read: readTab, deps: depsTab, impact: impactTab, graph: graphTab }[tab] || readTab;
  try { await render(pane, exp, live); }
  catch (e) { if (live()) pane.innerHTML = `<div class="empty err">${esc(e.message)}</div>`; }
}

async function readTab(pane, exp, live) {
  const id = exp.id, kind = exp.kind;
  if (kind === "claim") {
    const t = await api(`/artifacts/${enc(id)}/trace`);
    const evidence = await evidenceHTML(t.evidence);
    if (!live()) return;
    pane.innerHTML = `<div class="read"><div class="prose"><p style="font-size:20px;line-height:1.45">${esc(t.text)}</p>${evidence}</div>${dependentsCard(exp)}</div>`;
    return;
  }
  const doc = await api(`/artifacts/${enc(id)}/read`);
  if (kind === "source") {
    if (!live()) return;
    const m = doc.metadata;
    const note = m.on_disk === "present" ? (m.changed_since_build ? `<span class="pill stale">changed since build ${exp.last_changed_build}</span>` : `<span class="pill valid">same as build ${exp.last_changed_build}</span>`) : `<span class="pill deleted">${esc(m.on_disk)}</span>`;
    pane.innerHTML = `<p class="coverage">${note} · as it is on disk now · ${plural(exp.downstream_counts.passage || 0, "passage", "passages")}, ${plural(exp.downstream_counts.claim || 0, "claim", "claims")} and ${plural(exp.downstream_counts.page || 0, "page", "pages")} depend on it</p><div class="source-text">${esc(doc.text)}</div>`;
    return;
  }
  if (kind === "passage") {
    const claims = exp.dependents.filter((d) => d.kind === "claim");
    if (!live()) return;
    pane.innerHTML = `<div class="read"><div class="prose">
      <p class="coverage">From ${link(doc.metadata.source_id, exp.upstream_sources[0]?.path || doc.metadata.source_id)}${doc.metadata.heading ? ` · section “${esc(heading(doc.metadata.heading))}”` : ""}</p>
      <blockquote style="margin:0;font-size:17px">${esc(doc.text)}</blockquote></div>
      <section class="card"><h3>Claims extracted from it</h3>${claims.length ? `<ul class="list">${claims.map((c) => `<li><a class="row" href="#/a/${enc(c.id)}">${swatch("claim")}<span class="t">${esc(c.title)}</span><span></span></a></li>`).join("")}</ul>` : '<p class="muted">None: no sentence here named an entity.</p>'}</section></div>`;
    return;
  }
  if (kind === "entity" || kind === "relationship") {
    const claims = exp.depends_on.filter((d) => d.kind === "claim");
    if (!live()) return;
    pane.innerHTML = `<section class="card"><h3>${plural(claims.length, "claim", "claims")} about ${esc(exp.title)}</h3><ul class="list">${claims.map((c) =>
      `<li><a class="row" href="#/a/${enc(c.id)}">${swatch("claim")}<span class="t">${esc(c.title)}</span><span></span></a></li>`).join("")}</ul></section>`;
    return;
  }
  // page, summary, index: rendered Markdown; pages get citation provenance
  const trace = kind === "page" ? await api(`/artifacts/${enc(id)}/trace`) : null;
  if (!live()) return;
  const cited = trace ? trace.sentences.length - trace.uncited_sentences.length : 0;
  const coverage = trace ? `<p class="coverage">${fmt(cited)} of ${plural(trace.sentences.length, "sentence", "sentences")} cite a claim. Select a citation to see the exact source sentence.</p>` : "";
  pane.innerHTML = `<div class="read"><article class="prose">${coverage}${markdown(doc.text, { kind })}</article>
    ${trace ? `<aside class="card prov" id="prov" aria-live="polite"><h3>Where it comes from</h3><p class="muted">Select a citation such as <span class="cite">C1</span> in the text.</p></aside>` : dependentsCard(exp)}</div>`;
  if (!trace) return;
  const claims = Object.fromEntries(trace.claims.map((c) => [c.ref, c]));
  pane.querySelectorAll(".cite[data-ref]").forEach((btn) => btn.addEventListener("click", async () => {
    pane.querySelectorAll(".cite[aria-pressed='true']").forEach((b) => b.setAttribute("aria-pressed", "false"));
    pane.querySelectorAll(`.cite[data-ref="${btn.dataset.ref}"]`).forEach((b) => b.setAttribute("aria-pressed", "true"));
    const c = claims[btn.dataset.ref];
    const prov = $("#prov", pane);
    if (!c) { prov.innerHTML = `<h3>Where it comes from</h3><p class="err">${esc(btn.dataset.ref)} points to no claim of this page.</p>`; return; }
    prov.innerHTML = `<h3>${esc(c.ref)} · claim</h3><p class="claim">${link(c.claim_id, c.text)}</p><p class="loading">Loading the source…</p>`;
    if (matchMedia("(max-width: 900px)").matches) prov.scrollIntoView({ block: "start", behavior: "smooth" });
    const ev = await evidenceHTML(c.evidence);
    prov.innerHTML = `<h3>${esc(c.ref)} · claim</h3><p class="claim">${link(c.claim_id, c.text)}</p>${ev}`;
  }));
}

async function evidenceHTML(evidence) {
  const texts = await Promise.all(evidence.map((e) => api(`/artifacts/${enc(e.passage_id)}/read`).then((d) => d.text).catch(() => null)));
  return evidence.map((e, i) => `<div class="ev">
    <div class="ev-src">${swatch("source")}${link(e.source_id, e.source_path)}
      <span class="muted">registered in build ${e.source_version_build ?? "–"} · ${esc(e.source_raw_hash)}</span></div>
    ${e.heading ? `<p class="muted" style="margin:0 0 6px;font-size:13px">Section: ${esc(heading(e.heading))}</p>` : ""}
    <blockquote>${texts[i] ? highlight(texts[i], e.sentences) : e.sentences.map(esc).join(" ")}</blockquote>
    <p style="margin:6px 0 0;font-size:12.5px">${link(e.passage_id, "Open the passage")}</p></div>`).join("");
}

function dependentsCard(exp) {
  const used = exp.dependents || [];
  return `<section class="card"><h3>Used by</h3>${used.length ? `<ul class="list">${used.slice(0, 12).map((d) =>
    `<li><a class="row" href="#/a/${enc(d.id)}">${swatch(d.kind)}<span class="t">${esc(d.title)}</span><span class="id">${esc(d.kind)}</span></a></li>`).join("")}</ul>${used.length > 12 ? `<p class="muted">and ${fmt(used.length - 12)} more</p>` : ""}` : '<p class="muted">Nothing depends on it.</p>'}</section>`;
}

async function depsTab(pane, exp, live) {
  if (!live()) return;
  const rows = (items) => items.length ? `<ul class="list">${items.slice(0, 40).map((d) =>
    `<li><a class="row" href="#/a/${enc(d.id)}">${swatch(d.kind)}<span class="t">${esc(d.title)}</span><span class="id">${esc(d.edge)}</span></a></li>`).join("")}</ul>${items.length > 40 ? `<p class="muted">and ${fmt(items.length - 40)} more</p>` : ""}` : '<p class="muted">None.</p>';
  const counts = (o) => Object.entries(o || {}).map(([k, n]) => `<span class="pill">${swatch(k)} ${plural(n, k, k === "summary" ? "summaries" : k === "index" ? "index" : k + "s")}</span>`).join("");
  pane.innerHTML = `<div class="grid-2">
    <section class="card"><h3>Built from</h3>${rows(exp.depends_on)}
      <p style="margin:14px 0 6px" class="muted">Everything upstream</p><div class="chips">${counts(exp.upstream_counts) || '<span class="muted">nothing: this is a source</span>'}</div>
      ${exp.upstream_sources?.length ? `<p style="margin:14px 0 6px" class="muted">Source documents</p><div class="chips">${exp.upstream_sources.slice(0, 20).map((s) => `<a class="pill" href="#/a/${enc(s.id)}">${esc(s.path)}</a>`).join("")}</div>` : ""}</section>
    <section class="card"><h3>Used by</h3>${rows(exp.dependents)}
      <p style="margin:14px 0 6px" class="muted">Everything downstream</p><div class="chips">${counts(exp.downstream_counts) || '<span class="muted">nothing</span>'}</div></section>
  </div>
  ${exp.history?.length ? `<h2 class="section">History</h2><ul class="list card" style="padding:4px 12px">${exp.history.map((h) =>
    `<li class="row" style="grid-template-columns:90px 1fr auto"><span class="mono">v${h.version}</span><span>built in ${h.from_build ? `<a href="#/builds/${h.from_build}">#${h.from_build}</a>` : "–"}${h.to_build ? `, replaced in <a href="#/builds/${h.to_build}">#${h.to_build}</a>` : " · current"}</span><span class="id">${esc((h.state_hash || "").slice(0, 12))}</span></li>`).join("")}</ul>` : ""}
  ${Object.keys(exp.signatures || {}).length ? `<p class="muted" style="font-size:12.5px;margin-top:16px">Signatures: ${Object.entries(exp.signatures).map(([k, v]) => `${esc(k)} <span class="mono">${esc(v.slice(0, 16))}</span>`).join(" · ")}</p>` : ""}`;
}

async function impactTab(pane, exp, live) {
  const [d, st] = await Promise.all([api(`/artifacts/${enc(exp.id)}/downstream?limit=30`), api("/status")]);
  if (!live()) return;
  const counts = Object.fromEntries(LAYERS.map((l) => [l.kind, st.counts[l.kind] || 0]));
  const ov = { mode: "impact", layers: Object.fromEntries(LAYERS.map((l) => [l.kind, {
    changed: d.counts[l.kind] || 0, note: (n) => `<b>${fmt(n)}</b> would be invalidated` }])) };
  ov.layers[exp.kind] = { changed: 1, note: () => "<b>this one</b>" };
  pane.innerHTML = `<p class="lede">If this ${esc((KIND[exp.kind] || exp.kind).toLowerCase())} changed, <strong>${plural(d.total, "artifact", "artifacts")}</strong> would have to be checked. That is everything built from it, and nothing else.</p>
    ${stack(counts, ov)}
    <div class="grid-2" style="margin-top:28px">${Object.entries(d.by_kind).filter(([k]) => k !== "relationship").map(([k, items]) =>
      `<section class="card"><h3>${esc(KIND[k] || k)} · ${fmt(d.counts[k])}</h3><ul class="list">${items.map((a) =>
        `<li><a class="row" href="#/a/${enc(a.id)}">${swatch(a.kind)}<span class="t">${esc(a.title)}</span><span></span></a></li>`).join("")}</ul>${d.counts[k] > items.length ? `<p class="muted">and ${fmt(d.counts[k] - items.length)} more</p>` : ""}</section>`).join("") || '<div class="empty">Nothing is built from it.</div>'}</div>`;
}

async function graphTab(pane, exp, live) {
  const nb = await api(`/artifacts/${enc(exp.id)}/neighborhood?up=6&down=6&per_kind=6`);
  if (!live()) return;
  const nodes = nb.nodes.filter((n) => n.kind !== "relationship");
  const keep = new Set(nodes.map((n) => n.id));
  const edges = nb.edges.filter((e) => keep.has(e.dependency) && keep.has(e.dependent));
  const cols = [...new Set(nodes.map((n) => n.layer))].sort((a, b) => a - b);
  const colOf = Object.fromEntries(cols.map((l, i) => [l, i]));
  const avail = Math.max(600, pane.clientWidth - 30);
  const W = Math.max(150, Math.min(196, Math.floor((avail - 20) / Math.max(cols.length, 1))));
  const BOX = W - 26, H = 34, TOP = 34;
  const pos = {}, perCol = {};
  for (const n of nodes) {
    const c = colOf[n.layer];
    const row = (perCol[c] = (perCol[c] ?? -1) + 1);
    pos[n.id] = { x: 10 + c * W, y: TOP + row * H };
  }
  const height = TOP + Math.max(...Object.values(perCol).map((r) => r + 1), 1) * H + 40;
  const width = 20 + cols.length * W;
  const kindOfLayer = Object.fromEntries(nodes.map((n) => [n.layer, n.kind]));
  const hiddenByLayer = {};
  for (const [k, n] of Object.entries(nb.hidden)) {
    const node = nodes.find((x) => x.kind === k);
    if (node) hiddenByLayer[node.layer] = (hiddenByLayer[node.layer] || 0) + n;
  }
  const short = (s, n = Math.floor(BOX / 7.6)) => (s.length > n ? s.slice(0, n - 1) + "…" : s);
  const edgeSvg = edges.map((e) => {
    const a = pos[e.dependency], b = pos[e.dependent];
    const x1 = a.x + BOX, y1 = a.y + 13, x2 = b.x, y2 = b.y + 13, mx = (x1 + x2) / 2;
    const hot = e.dependency === nb.root || e.dependent === nb.root;
    return `<path class="g-edge${hot ? " hot" : ""}" d="M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}"/>`;
  }).join("");
  const nodeSvg = nodes.map((n) => {
    const p = pos[n.id];
    return `<a href="#/a/${enc(n.id)}" class="g-node${n.id === nb.root ? " root" : ""}"><title>${esc(n.title)} (${esc(n.id)})</title>
      <rect class="box" x="${p.x}" y="${p.y}" width="${BOX}" height="26" rx="5"/>
      <rect x="${p.x}" y="${p.y}" width="5" height="26" rx="2" fill="var(--l-${n.kind})"/>
      <text x="${p.x + 12}" y="${p.y + 17}">${esc(short(n.title))}</text></a>`;
  }).join("");
  const heads = cols.map((l, i) => `<text class="g-col" x="${10 + i * W}" y="18">${esc(KIND[kindOfLayer[l]] || "")}</text>`).join("");
  const more = cols.map((l, i) => hiddenByLayer[l] ? `<text class="g-more" x="${10 + i * W}" y="${TOP + ((perCol[i] ?? 0) + 1) * H + 12}">+ ${fmt(hiddenByLayer[l])} more</text>` : "").join("");
  pane.innerHTML = `<p class="coverage">Sources on the left, the index on the right; edges point from what something is built from to what is built from it. Up to six of each kind are shown${nb.hidden.relationship ? "; relationships are left out" : ""}.</p>
    <div class="graph-wrap"><svg width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" role="img" aria-label="Dependency graph around ${esc(exp.title)}">${heads}${edgeSvg}${nodeSvg}${more}</svg></div>`;
}

async function changes(view) {
  const live = begin(view);
  const st = await api("/status");
  if (!live()) return;
  if (st.up_to_date) {
    view.innerHTML = `<p class="eyebrow">${swatch("source")} Changes</p><h1 class="title">Nothing to rebuild.</h1>
      <div class="empty">The knowledge base matches all ${plural(st.counts.source || 0, "source", "sources")}. Edit a document in
      <code>${esc(st.source_root)}</code>, then reload: this page shows exactly what a compile would redo, before anything runs.</div>`;
    return;
  }
  const [plan, chk] = await Promise.all([api("/plan"), api("/check")]);
  if (!live()) return;
  const oc = plan.operation_counts || {}, ops = plan.operations || {};
  const calls = llmCalls(plan);
  const files = [...chk.modified.map((p) => ["modified", p]), ...chk.new.map((p) => ["new", p]),
    ...chk.deleted.map((p) => ["deleted", p]), ...chk.renamed.map(([a, b]) => ["renamed", `${a} → ${b}`])];
  const listed = [ops.regenerate_page, ops.cutoff_page, ops.regenerate_summary, ops.rerender_summary, ops.delete_page]
    .flatMap((x) => (x || []).slice(0, 30));
  const names = listed.length ? await api(`/titles?${listed.map((i) => `id=${enc(i)}`).join("&")}`).catch(() => ({})) : {};
  if (!live()) return;
  const pageList = (ids, title) => ids?.length ? `<section class="card"><h3>${title} · ${fmt(ids.length)}</h3><ul class="list">${ids.slice(0, 30).map((i) =>
    `<li><a class="row" href="#/a/${enc(i)}">${swatch(i.startsWith("sum:") ? "summary" : "page")}<span class="t">${esc(names[i] || i.split(":")[1])}${names[i] ? "" : ' <span class="pill valid">new</span>'}</span><span class="id">${esc(i)}</span></a></li>`).join("")}</ul>${ids.length > 30 ? `<p class="muted">and ${fmt(ids.length - 30)} more</p>` : ""}</section>` : "";
  view.innerHTML = `
    <p class="eyebrow">${swatch("source")} Changes since build ${st.head}</p>
    <h1 class="title">${files.length ? plural(files.length, "source changed", "sources changed") : "No source changed"}. ${plural(calls, "LLM call", "LLM calls")} to catch up.</h1>
    ${st.outdated?.length ? `<p class="lede">${esc(st.outdated.join("; "))}.</p>` : ""}
    <p class="lede">This is the plan a compile would run, computed without writing or spending anything${plan.exact ? "" : " (an estimate: the facts in new text are estimated until the model reads it)"}.
      <button type="button" class="pill" id="full" style="cursor:pointer;background:var(--surface)">Compare with a full rebuild</button> <span id="full-out" class="muted"></span></p>
    <ul class="list card" style="padding:4px 12px;margin:18px 0">${files.map(([how, p]) =>
      `<li class="row" style="grid-template-columns:auto 1fr">${`<span class="pill ${how === "deleted" ? "deleted" : how === "new" ? "valid" : "changed"}">${how}</span>`}<span class="mono">${how === "modified" ? link(p, p) : esc(p)}</span></li>`).join("")}</ul>
    ${stack(st.counts, overlay(plan, "pending"))}
    ${(oc.regenerate_section || oc.cutoff_section) ? `<p class="coverage" style="margin:14px 0 0 126px">Hub pages and large topics are built in sections: <strong>${fmt(oc.regenerate_section || 0)}</strong> to rewrite, ${fmt(oc.cutoff_section || 0)} reused.</p>` : ""}
    <div class="grid-2" style="margin-top:30px">
      ${pageList(ops.regenerate_page, "Pages to regenerate")}
      ${pageList(ops.cutoff_page, "Pages checked, found unchanged")}
      ${pageList([...(ops.regenerate_summary || []), ...(ops.rerender_summary || [])], "Summaries to update")}
      ${pageList(ops.delete_page, "Pages to delete")}
    </div>
    <div class="callout">This UI is read-only. Apply the plan with <code>dakc compile</code>, or ask an agent through <code>dakc mcp</code>.</div>`;
  $("#full", view).addEventListener("click", async (ev) => {
    ev.target.disabled = true;
    $("#full-out", view).textContent = "Planning a full rebuild…";
    try {
      const full = await api("/plan?full=true");
      $("#full-out", view).innerHTML = `A full rebuild would be <strong>${plural(llmCalls(full), "LLM call", "LLM calls")}</strong>${calls ? `, ${fmt(Math.round(llmCalls(full) / calls))} times as many` : ""}.`;
    } catch (e) { $("#full-out", view).textContent = e.message; }
  });
}

async function builds(view, selected) {
  const live = begin(view);
  const [list, st] = await Promise.all([api("/builds?limit=50"), api("/status")]);
  if (!live()) return;
  if (!list.length) { view.innerHTML = `<h1 class="title">No builds yet.</h1><div class="empty">Run <code>dakc compile</code> to create the first one.</div>`; return; }
  const id = selected ? Number(selected) : list[0].id;
  const b = list.find((x) => x.id === id) || list[0];
  view.innerHTML = `<p class="eyebrow">${swatch("index")} History</p><h1 class="title">Every build, and what it changed.</h1>
    <div class="builds" style="margin-top:22px"><ul class="list card timeline" style="padding:4px 8px">${list.map((x) => buildRow(x, st.head, x.id === b.id)).join("")}</ul>
    <section id="build-detail"><p class="loading">Loading…</p></section></div>`;
  const prev = b.parent ?? list.filter((x) => x.id < b.id && x.status === "committed")[0]?.id ?? 0;
  const s = b.stats || {};
  let diff = {};
  try { diff = await api(`/diff/${prev}/${b.id}?titles=true`); } catch (e) { diff = {}; }
  if (!live()) return;
  const ops = Object.entries(s.operation_counts || {}).filter(([, n]) => n);
  $("#build-detail", view).innerHTML = `<div class="card">
    <h3>Build #${b.id}</h3>
    <div class="chips" style="margin-bottom:12px">${statusPill(b.status, st.head, b.id)}<span class="pill">${esc(b.strategy)}</span>
      ${s.usage ? `<span class="pill">${plural(s.usage.llm_calls, "LLM call", "LLM calls")}</span>` : ""}
      ${s.duration_s != null ? `<span class="pill">${s.duration_s.toFixed(2)} s</span>` : ""}</div>
    ${b.error ? `<p class="err">${esc(b.error)}</p>` : ""}
    ${ops.length ? `<div class="ops">${ops.map(([k, n]) => `<span class="pill"><strong class="num">${fmt(n)}</strong> ${esc(OPS[k] || k.replaceAll("_", " "))}</span>`).join("")}</div>` : '<p class="muted">No operations recorded.</p>'}
    <h2 class="section" style="margin-top:22px">Compared with build ${prev ? `#${prev}` : "zero (empty)"}</h2>
    ${Object.keys(diff).length ? LAYERS.slice().reverse().filter((l) => diff[l.kind]).map((l) => {
      const items = diff[l.kind];
      const by = { created: 0, updated: 0, deleted: 0 };
      items.forEach((x) => (by[x.change] = (by[x.change] || 0) + 1));
      const pillCls = { created: "valid", updated: "changed", deleted: "deleted" };
      return `<p class="diff-kind">${swatch(l.kind)} ${esc(l.label)} <span class="muted" style="font-weight:400">${["created", "updated", "deleted"].filter((c) => by[c]).map((c) => `${fmt(by[c])} ${c}`).join(" · ")}</span></p>
        <ul class="list diff-list">${items.slice(0, 8).map((x) => `<li>${x.change === "deleted"
          ? `<span class="row">${swatch(l.kind)}<span class="t">${esc(x.title)}</span><span class="pill deleted">deleted</span></span>`
          : `<a class="row" href="#/a/${enc(x.id)}">${swatch(l.kind)}<span class="t">${esc(x.title)}</span><span class="pill ${pillCls[x.change]}">${x.change}</span></a>`}</li>`).join("")}</ul>
        ${items.length > 8 ? `<p class="muted" style="margin:4px 0 0;font-size:13px">and ${fmt(items.length - 8)} more</p>` : ""}`;
    }).join("") : '<p class="muted">No artifacts changed.</p>'}</div>`;
}

// ------------------------------------------------------------------ conflicts (RFC 0002)
const CONFLICT_KIND = { conflict: "Conflict", possible: "Possible conflict", scoped: "Scoped difference" };
const conflictPill = (c) => c.accepted != null ? '<span class="pill accepted">accepted</span>'
  : `<span class="pill ${esc(c.kind)}">${esc(c.kind === "possible" ? "possible" : c.kind)}</span>`;
// A negation's two values are "is stated" and "is negated": say it plainly.
const valueLabel = (v) => ({ "is stated": "Yes", "is negated": "No" }[v] ?? v);
const docsOf = (v) => [...new Set(v.locations.map((l) => l.path))];

async function conflicts(view, ident, params) {
  if (ident) return conflictDetail(view, ident);
  const all = params.get("show") === "all";
  const live = begin(view);
  const [st, found] = await Promise.all([api("/status"), api(`/conflicts?scoped=${all}&accepted=${all}`)]);
  if (!live()) return;
  const open = found.filter((c) => (c.kind === "conflict" || c.kind === "possible") && c.accepted == null);
  const head = open.length ? `${plural(open.length, "open conflict", "open conflicts")} between documents.`
    : "No open conflicts between documents.";
  const seg = `<div class="seg" role="group" aria-label="Show">${[["open", "Open"], ["all", "With scoped and accepted"]].map(([k, label]) =>
    `<button type="button" data-show="${k}" aria-pressed="${(k === "all") === all}">${label}</button>`).join("")}</div>`;
  const rows = found.map((c) => `<li><a class="row conflict-row" href="#/conflicts/${enc(c.id)}">
      ${conflictPill(c)}
      <span class="t"><span class="prop">${esc(c.property)}</span>
        <span class="sub">${c.values.map((v) => `<b>${esc(valueLabel(v.value))}</b> ${esc(docsOf(v).join(", "))}`).join('<span class="vs" aria-label="against"> ≠ </span>')}</span></span>
      <span class="id">since #${c.since_build}</span></a></li>`).join("");
  view.innerHTML = `
    <p class="eyebrow"><span class="swatch" style="background:var(--deleted)"></span> Conflicts · build ${st.head || "–"}</p>
    <h1 class="title">${esc(head)}</h1>
    <p class="lede">Statements in different documents that cannot both be true. DAKC keeps both claims, each with its own
      source, and never picks a side: a conflict closes when the documents agree.</p>
    <div class="toolbar">${seg}</div>
    ${found.length ? `<ul class="list card" style="padding:4px 12px">${rows}</ul>`
      : `<div class="empty">${all ? "Nothing at all: no two documents give different values for the same property, in any scope."
        : `Nothing open. ${st.counts.claim ? `DAKC compared ${plural(st.counts.claim, "claim", "claims")}.` : ""}
          <a href="#/conflicts?show=all">Show scoped differences</a>, such as one release against another.`}
        <p class="muted" style="margin:10px 0 0;font-size:13.5px">The rules compare statements of the same shape. With
          <code>[conflicts] verifier = "llm"</code> in <code>dakc.toml</code>, a model also checks related statements
          worded differently.</p></div>`}`;
  view.querySelectorAll("[data-show]").forEach((b) => b.addEventListener("click", () => {
    location.hash = b.dataset.show === "all" ? "#/conflicts?show=all" : "#/conflicts";
  }));
}

async function conflictDetail(view, ident) {
  const live = begin(view);
  let c;
  try { c = await api(`/conflicts/${enc(ident)}`); }
  catch (e) {
    if (!live()) return;
    view.innerHTML = `<p class="eyebrow">Conflicts</p><h1 class="title">${esc(ident)}</h1><div class="empty">${esc(e.message)}
      It may have closed: <a href="#/conflicts">see the open ones</a>.</div>`;
    return;
  }
  if (!live()) return;
  const label = c.accepted != null ? "Accepted conflict" : CONFLICT_KIND[c.kind] || c.kind;
  const faceoff = c.kind !== "possible" && c.values.length <= 4
    ? `<div class="faceoff" aria-label="The values the documents give">${c.values.map((v) =>
      `<span class="val">${esc(valueLabel(v.value))}</span>`).join('<span class="ne" aria-hidden="true">≠</span>')}</div>` : "";
  const card = (v) => `<section class="card value-card">
      <h3>${c.kind === "possible" ? "One statement" : esc({ "is stated": "States it", "is negated": "Denies it" }[v.value] ?? v.value)}</h3>
      ${v.locations.map((l) => `<div class="loc">
        <div class="loc-src">${swatch("source")}<a class="mono" href="#/a/${enc(l.path)}">${esc(l.path)}${l.line ? `:${l.line}` : ""}</a>
          <span class="pill ${l.scope === "current" ? "" : "scope"}">${esc(l.scope)}</span></div>
        ${l.heading ? `<p class="muted loc-heading">${esc(heading(l.heading))}</p>` : ""}
        <blockquote>${esc(l.evidence || l.sentence)}</blockquote>
        <p class="loc-links">${link(l.claim_id, "Trace the claim")} · ${link(l.passage_id, "Open the passage")}</p></div>`).join("")}
    </section>`;
  // Which document describes another time is the reader's call: offer each one. Preselect the
  // value stated in the fewest places (on a tie, the last), the likelier odd one out.
  const docs = [...new Set(c.values.flatMap((v) => v.locations.map((l) => l.path)))];
  const odd = c.values.reduce((a, v) => (v.locations.length <= a.locations.length ? v : a), c.values[0]);
  const scopePath = odd?.locations[0]?.path || docs[0] || "docs/old/**";
  const scopeText = (path) => `[[conflicts.scope]]\npath = "${path}"\nlabel = "release 1"        # or status = "past"`;
  const snippet = (id, text) => `<div class="snippet"><pre id="${id}">${esc(text)}</pre>
      <button type="button" class="pill copy" data-copy="${id}">Copy</button></div>`;
  const affected = Object.entries(c.affected || {}).map(([k, items]) =>
    `<p class="muted" style="margin:12px 0 6px">${esc(KIND[k] || k)} · ${fmt(items.length)}</p><div class="chips">${items.slice(0, 24).map((a) =>
      `<a class="pill" href="#/a/${enc(a.id)}">${swatch(a.kind)} ${esc(a.title)}</a>`).join("")}${items.length > 24 ? `<span class="muted">and ${fmt(items.length - 24)} more</span>` : ""}</div>`).join("");
  view.innerHTML = `
    <p class="eyebrow"><span class="swatch" style="background:var(--${c.kind === "possible" ? "stale" : c.kind === "scoped" ? "muted" : "deleted"})"></span>
      ${esc(label)} · <span class="mono">${esc(c.id)}</span> · since <a href="#/builds/${c.since_build}">build ${c.since_build}</a></p>
    <h1 class="title">${esc(c.property)}</h1>
    ${faceoff}
    <p class="lede">${esc(c.reason.charAt(0).toUpperCase() + c.reason.slice(1))}.</p>
    ${c.kind === "possible" ? '<div class="callout warn">Judged by the conflict verifier, a model, not by DAKC\'s rules. Check both statements.</div>' : ""}
    ${c.accepted != null ? `<div class="callout">Accepted in <code>dakc.toml</code>: ${esc(c.accepted || "no reason given")}</div>` : ""}
    <div class="values">${c.values.map(card).join("")}</div>
    <div class="grid-2" style="margin-top:28px">
      <section class="card"><h3>Built on these statements</h3>${affected || '<p class="muted">No page or summary uses them yet.</p>'}</section>
      <section class="card resolve"><h3>${c.kind === "scoped" ? "Why it is not a conflict" : "Resolve it"}</h3>
        ${c.kind === "scoped" ? `<p>The values apply to different scopes: a release, a time, an environment, or a statement that is not
          a fact today. Nothing needs fixing. If the documents should agree, remove the scope from the text.</p>` : `
        <p><b>Fix the text.</b> Edit either document and compile. The conflict closes when they agree.</p>
        <p><b>A difference of time, release or environment?</b> Say so in the sentence or its heading (“Before
          version 2”), or tell DAKC which document describes another time:</p>
        ${docs.length > 1 ? `<div class="seg doc-pick" role="group" aria-label="Document the rule is for">${docs.slice(0, 4).map((d) =>
          `<button type="button" data-doc="${esc(d)}" aria-pressed="${d === scopePath}">${esc(d.split("/").pop())}</button>`).join("")}</div>` : ""}
        ${snippet("scope-snippet", scopeText(scopePath))}
        <p><b>Keep both as they are.</b> Record that someone reviewed it:</p>
        ${snippet("accept-snippet", `[[conflicts.accept]]\nid = "${c.id}"\nreason = "…"`)}
        <p class="muted" style="font-size:13px;margin:10px 0 0">This UI is read-only: add these to <code>dakc.toml</code>, then compile.</p>`}
      </section>
    </div>`;
  view.querySelectorAll("[data-doc]").forEach((b) => b.addEventListener("click", () => {
    view.querySelectorAll("[data-doc]").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
    $("#scope-snippet", view).textContent = scopeText(b.dataset.doc);
  }));
  view.querySelectorAll("[data-copy]").forEach((b) => b.addEventListener("click", async () => {
    const text = $(`#${b.dataset.copy}`, view).textContent;
    try { await navigator.clipboard.writeText(text); b.textContent = "Copied"; }
    catch (e) { // no clipboard (an insecure origin, or permission denied): select the text instead
      const range = document.createRange();
      range.selectNodeContents($(`#${b.dataset.copy}`, view));
      getSelection().removeAllRanges();
      getSelection().addRange(range);
      b.textContent = "Selected: press Ctrl+C";
    }
    setTimeout(() => { b.textContent = "Copy"; }, 2000);
  }));
}

// Open conflicts a claim, passage, page or document is part of: a reader should know a fact is disputed.
async function conflictNotice(exp) {
  if (!["claim", "passage", "page", "source"].includes(exp.kind)) return "";
  let open, claims = new Set();
  try {
    open = await api("/conflicts");
    if (!open.length) return "";
    if (exp.kind === "page") claims = new Set((await api(`/artifacts/${enc(exp.id)}/trace`)).claims.map((c) => c.claim_id));
  } catch (e) { return ""; }
  const hit = open.filter((c) => c.values.some((v) => v.locations.some((l) =>
    (exp.kind === "claim" && l.claim_id === exp.id) || (exp.kind === "passage" && l.passage_id === exp.id)
    || (exp.kind === "source" && l.path === exp.title) || (exp.kind === "page" && claims.has(l.claim_id)))));
  if (!hit.length) return "";
  const what = { claim: "This claim is", passage: "A claim from this passage is", page: "A claim on this page is",
    source: "A statement in this document is" }[exp.kind];
  return `<div class="callout conflict">${what} contradicted by another document:
    ${hit.slice(0, 3).map((c) => `<a href="#/conflicts/${enc(c.id)}">${esc(c.property)}</a>`).join("; ")}${hit.length > 3 ? ` and ${fmt(hit.length - 3)} more` : ""}.</div>`;
}

// ------------------------------------------------------------------ shell
function setNav(section) {
  document.querySelectorAll(".rail a").forEach((a) => {
    if (a.dataset.nav === section) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
}

async function refreshShell() {
  try {
    const st = await api("/status");
    const name = st.workspace.split("/").filter(Boolean).pop();
    $("#ws").innerHTML = `<b>${esc(name)}</b> · build ${st.head || "–"}`;
    const changed = sum([st.source_changes.new, st.source_changes.modified, st.source_changes.deleted, st.source_changes.renamed]);
    const badge = $("#changes-badge");
    badge.hidden = !changed;
    badge.textContent = changed;
    const cb = $("#conflicts-badge");
    cb.hidden = !st.conflicts_open;
    cb.textContent = st.conflicts_open || "";
    document.title = `DAKC · ${name}`;
  } catch (e) { $("#ws").textContent = "workspace unavailable"; }
}

async function route() {
  const h = location.hash.slice(1) || "/";
  const [path, qs] = h.split("?");
  const params = new URLSearchParams(qs || "");
  const parts = path.split("/").filter(Boolean);
  const view = $("#view");
  const section = { explore: "explore", a: "explore", changes: "changes", builds: "builds", conflicts: "conflicts" }[parts[0]] || "overview";
  setNav(section);
  if (parts[0] !== "explore") window.scrollTo(0, 0); // explore re-renders while typing; keep its scroll
  try {
    if (!parts.length) await overview(view);
    else if (parts[0] === "explore") await explore(view, params);
    else if (parts[0] === "a") await artifact(view, decodeURIComponent(parts.slice(1).join("/")), params);
    else if (parts[0] === "changes") await changes(view);
    else if (parts[0] === "builds") await builds(view, parts[1]);
    else if (parts[0] === "conflicts") await conflicts(view, parts[1] ? decodeURIComponent(parts.slice(1).join("/")) : null, params);
    else view.innerHTML = `<h1 class="title">No such page.</h1><p><a href="#/">Back to the overview</a></p>`;
  } catch (e) {
    view.innerHTML = `<h1 class="title">Could not load this view.</h1><div class="empty err">${esc(e.message)}</div>`;
  }
}

$("#top-search").addEventListener("submit", (e) => {
  e.preventDefault();
  const q = new FormData(e.target).get("q") || "";
  location.hash = `#/explore?kind=page&q=${enc(q)}`;
});
$("#theme").addEventListener("click", () => {
  const dark = document.documentElement.dataset.theme
    ? document.documentElement.dataset.theme === "dark"
    : matchMedia("(prefers-color-scheme: dark)").matches;
  const next = dark ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("dakc-theme", next); } catch (e) { /* storage unavailable */ }
});
document.addEventListener("keydown", (e) => {
  if (e.key === "/" && !/input|textarea/i.test(document.activeElement?.tagName || "")) {
    e.preventDefault();
    $("#top-search input").focus();
  }
});
window.addEventListener("hashchange", route);
refreshShell();
route();
