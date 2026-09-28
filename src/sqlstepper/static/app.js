"use strict";

// ---------------------------------------------------------------------------
// state
// ---------------------------------------------------------------------------
const $ = (s) => document.querySelector(s);
const S = {
  trace: null,        // last /api/trace response
  flat: [],           // [{step, pipe, stmtIndex, error?}] in execution order
  cur: -1,            // index into flat
  prog: 0,            // animation units shown in the current step
  playing: false,
  stepOnly: false,    // "next step": stop when the current step finishes
  timer: null,
  view: null,         // DOM refs of the rendered step
  lineBps: new Set(),
  stepBps: new Set(),
  stale: false,
  tables: [],
  examples: [],
};
const DELAYS = [1400, 1000, 700, 450, 300, 190, 120, 70, 35, 12];
const KEYWORDS = ("SELECT FROM WHERE GROUP BY HAVING ORDER LIMIT OFFSET JOIN LEFT RIGHT FULL OUTER INNER CROSS " +
  "NATURAL SEMI ANTI ASOF POSITIONAL LATERAL ON USING AS AND OR NOT IN IS NULL LIKE ILIKE BETWEEN CASE WHEN THEN ELSE " +
  "END DISTINCT UNION ALL INTERSECT EXCEPT WITH RECURSIVE OVER PARTITION ROWS RANGE PRECEDING FOLLOWING UNBOUNDED " +
  "CURRENT ROW WINDOW QUALIFY INSERT INTO VALUES UPDATE SET DELETE CREATE TABLE VIEW DROP ALTER REPLACE EXISTS ASC " +
  "DESC NULLS FIRST LAST TRUE FALSE CAST FILTER ROLLUP CUBE GROUPING SETS PIVOT UNPIVOT EXCLUDE RETURNING " +
  "MATERIALIZED TEMP TEMPORARY OR IF").split(" ");
const KW_RE = new RegExp("\\b(" + KEYWORDS.join("|") + ")\\b", "gi");

// ---------------------------------------------------------------------------
// persistence (per-browser conveniences only)
// ---------------------------------------------------------------------------
function load() {
  try { return JSON.parse(localStorage.getItem("sqlstepper") || "{}"); } catch { return {}; }
}
function save() {
  try {
    localStorage.setItem("sqlstepper", JSON.stringify({
      sql: $("#sql").value,
      lineBps: [...S.lineBps],
      stepBps: [...S.stepBps],
      sandbox: $("#sandbox").checked,
      speed: $("#speed").value,
      animate: $("#animate").checked,
      theme: document.documentElement.dataset.theme || "",
      editorH: document.documentElement.style.getPropertyValue("--editor-h") || "",
    }));
  } catch { /* storage unavailable: settings just won't persist */ }
}

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------
async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  let data;
  try { data = await res.json(); } catch { throw new Error(`伺服器回應錯誤（${res.status}）`); }
  if (!res.ok || (data && data.error && !("statements" in data))) throw new Error(data.error || res.statusText);
  return data;
}
function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}
function el(tag, attrs = {}, html = "") {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  if (html) e.innerHTML = html;
  return e;
}
function toast(msg, isErr = false) {
  const t = el("div", { class: isErr ? "err" : "" });
  t.textContent = msg;
  $("#toast").appendChild(t);
  setTimeout(() => t.remove(), isErr ? 7000 : 3500);
}
function setStatus(text, cls = "") {
  const s = $("#status");
  s.textContent = text;
  s.className = cls;
}
function fmtCell(v) {
  if (v === null || v === undefined) return { html: '<span class="null">NULL</span>', num: false };
  if (typeof v === "number") return { html: esc(v), num: true };
  if (typeof v === "boolean") return { html: v ? "true" : "false", num: false };
  if (typeof v === "object") return { html: esc(JSON.stringify(v)), num: false, title: JSON.stringify(v) };
  const s = String(v);
  return { html: esc(s), num: false, title: s.length > 40 ? s : undefined };
}
function groupColor(g) {
  if (g === null || g === undefined) return "transparent";
  const hue = (g * 137.508) % 360;
  return `hsl(${hue.toFixed(0)} 70% 52%)`;
}

// ---------------------------------------------------------------------------
// editor
// ---------------------------------------------------------------------------
const ed = $("#sql");
function highlight() {
  const src = ed.value;
  let out = "";
  const re = /(--[^\n]*|\/\*[\s\S]*?\*\/)|('(?:[^']|'')*'?)|("(?:[^"]|"")*"?)|(\b\d+(?:\.\d+)?\b)/g;
  let last = 0;
  for (const m of src.matchAll(re)) {
    out += esc(src.slice(last, m.index)).replace(KW_RE, '<span class="kw">$1</span>');
    if (m[1]) out += `<span class="cmt">${esc(m[1])}</span>`;
    else if (m[2]) out += `<span class="str">${esc(m[2])}</span>`;
    else if (m[3]) out += esc(m[3]);
    else out += `<span class="num">${esc(m[4])}</span>`;
    last = m.index + m[0].length;
  }
  out += esc(src.slice(last)).replace(KW_RE, '<span class="kw">$1</span>');
  $("#hl").innerHTML = out + "\n";
  renderGutter();
}
function lineCount() { return ed.value.split("\n").length; }
function steppableLines() {
  const lines = new Set();
  for (const f of S.flat) if (f.step && f.step.line) lines.add(f.step.line);
  return lines;
}
function renderGutter() {
  const n = lineCount();
  const stepLines = S.stale ? new Set() : steppableLines();
  const curLine = currentLine();
  const g = $("#gutter");
  let html = "";
  for (let i = 1; i <= n; i++) {
    const cls = ["ln", S.lineBps.has(i) ? "bp" : "", stepLines.has(i) ? "steppable" : "", curLine === i ? "current" : ""]
      .filter(Boolean).join(" ");
    html += `<div class="${cls}" data-line="${i}"><span class="dot"></span>${i}</div>`;
  }
  g.innerHTML = html;
  syncScroll();
}
function currentLine() {
  if (S.stale || S.cur < 0 || !S.flat[S.cur]) return null;
  const f = S.flat[S.cur];
  return f.step ? f.step.line : f.line;
}
function syncScroll() {
  $("#hl").scrollTop = ed.scrollTop;
  $("#hl").scrollLeft = ed.scrollLeft;
  $("#gutter").scrollTop = ed.scrollTop;
  const hl = $("#line-hl");
  const line = currentLine();
  if (line) {
    hl.style.display = "block";
    hl.style.top = `${10 + (line - 1) * 20 - ed.scrollTop}px`;
  } else {
    hl.style.display = "none";
  }
}
function revealLine(line) {
  if (!line) return;
  const top = 10 + (line - 1) * 20;
  if (top < ed.scrollTop + 10 || top > ed.scrollTop + ed.clientHeight - 30) {
    ed.scrollTop = Math.max(0, top - ed.clientHeight / 3);
  }
}
function toggleLineBp(line) {
  if (S.lineBps.has(line)) S.lineBps.delete(line); else S.lineBps.add(line);
  renderGutter();
  renderPlan();
  save();
}
ed.addEventListener("input", () => {
  if (S.trace && !S.stale) {
    pause();
    S.stale = true;
    setStatus("SQL 已修改，按 ▶ 執行 重新計算", "warn");
    updateButtons();
  }
  highlight();
  save();
});
ed.addEventListener("scroll", syncScroll);
ed.addEventListener("keydown", (e) => {
  if (e.key === "Tab") {
    e.preventDefault();
    const { selectionStart: a, selectionEnd: b } = ed;
    ed.setRangeText("  ", a, b, "end");
    ed.dispatchEvent(new Event("input"));
  }
});
$("#gutter").addEventListener("click", (e) => {
  const ln = e.target.closest(".ln");
  if (ln) toggleLineBp(Number(ln.dataset.line));
});

// ---------------------------------------------------------------------------
// tables sidebar
// ---------------------------------------------------------------------------
function renderTables() {
  const box = $("#tables");
  box.innerHTML = "";
  if (!S.tables.length) {
    box.innerHTML = '<div class="plan-empty">沒有資料表。匯入檔案或按「重設範例」。</div>';
    return;
  }
  for (const t of S.tables) {
    const item = el("div", { class: "tbl" });
    const head = el("div", { class: "tbl-head", title: "點擊展開欄位，雙擊插入名稱" },
      `<span class="tbl-name">${esc(t.name)}</span><span class="tbl-count">${t.kind === "view" ? "view" : t.rows + " 列"}</span>`);
    head.addEventListener("click", () => item.classList.toggle("open"));
    head.addEventListener("dblclick", () => insertText(t.name));
    const peek = el("button", { class: "tbl-act", title: "預覽資料" }, "👁");
    peek.addEventListener("click", (e) => { e.stopPropagation(); preview(t.name); });
    const drop = el("button", { class: "tbl-act", title: "刪除資料表" }, "✕");
    drop.addEventListener("click", (e) => { e.stopPropagation(); dropTable(t.name); });
    head.append(peek, drop);
    const cols = el("ul", { class: "tbl-cols" },
      t.columns.map((c) => `<li><span>${esc(c.name)}</span><span class="ty">${esc(c.type)}</span></li>`).join(""));
    item.append(head, cols);
    box.appendChild(item);
  }
}
function insertText(text) {
  ed.focus();
  ed.setRangeText(text, ed.selectionStart, ed.selectionEnd, "end");
  ed.dispatchEvent(new Event("input"));
}
async function refreshState() {
  const st = await api("/api/state");
  S.tables = st.tables;
  S.examples = st.examples;
  renderTables();
  return st;
}
async function preview(name) {
  pause();
  try {
    const p = await api("/api/preview?name=" + encodeURIComponent(name));
    S.cur = -1;
    S.view = null;
    renderPlan();
    const stage = $("#stage");
    stage.innerHTML = "";
    stage.appendChild(el("div", { class: "stage-head" },
      `<div class="crumb">資料表預覽</div><div class="stage-title">${esc(name)}</div>` +
      `<p class="explain">共 ${p.total} 列${p.total > p.rows.length ? `，顯示前 ${p.rows.length} 列` : ""}。</p>`));
    const panels = el("div", { class: "panels" });
    panels.appendChild(buildPanel(p, {}).el);
    stage.appendChild(panels);
  } catch (err) { toast(err.message, true); }
}
async function dropTable(name) {
  if (!confirm(`確定刪除資料表 ${name}？`)) return;
  try {
    const st = await api("/api/drop", { method: "POST", body: JSON.stringify({ name }) });
    S.tables = st.tables;
    renderTables();
    toast(`已刪除 ${name}`);
  } catch (err) { toast(err.message, true); }
}
async function importFile(file) {
  const dbLike = /\.(duckdb|ddb|db|sqlite3?)$/i.test(file.name);
  let name = "";
  if (!dbLike) {
    const def = file.name.replace(/\.[^.]+$/, "").replace(/\W+/g, "_").toLowerCase() || "imported";
    name = prompt("匯入後的資料表名稱：", def);
    if (name === null) return;
  }
  setStatus(`匯入 ${file.name}…`);
  try {
    const q = `filename=${encodeURIComponent(file.name)}&name=${encodeURIComponent(name)}`;
    const st = await api("/api/import?" + q, { method: "POST", body: file });
    S.tables = st.tables;
    renderTables();
    toast(`已匯入：${st.created.join(", ") || "（沒有資料表）"}`);
    setStatus("");
  } catch (err) {
    toast(err.message, true);
    setStatus("匯入失敗", "err");
  }
}
$("#import-btn").addEventListener("click", () => $("#file").click());
$("#file").addEventListener("change", (e) => {
  for (const f of e.target.files) importFile(f);
  e.target.value = "";
});
$("#reset-btn").addEventListener("click", async () => {
  if (!confirm("刪除所有資料表並重新載入範例資料？")) return;
  try {
    const st = await api("/api/reset", { method: "POST" });
    S.tables = st.tables;
    renderTables();
    toast("已重設範例資料");
  } catch (err) { toast(err.message, true); }
});
for (const target of [$("#sidebar"), document.body]) {
  target.addEventListener("dragover", (e) => { e.preventDefault(); $("#sidebar").classList.add("drag"); });
  target.addEventListener("dragleave", () => $("#sidebar").classList.remove("drag"));
  target.addEventListener("drop", (e) => {
    e.preventDefault();
    $("#sidebar").classList.remove("drag");
    for (const f of e.dataTransfer.files) importFile(f);
  });
}

// ---------------------------------------------------------------------------
// plan (left list of steps)
// ---------------------------------------------------------------------------
function flatten(trace) {
  const flat = [];
  trace.statements.forEach((st, si) => {
    for (const p of st.pipelines) for (const s of p.steps) flat.push({ step: s, pipe: p, stmtIndex: si });
    if (st.error) flat.push({ error: st.error, stmtIndex: si, line: st.line, pipe: null });
  });
  if (trace.error && !trace.statements.length) flat.push({ error: trace.error, stmtIndex: 0, line: null, pipe: null });
  return flat;
}
function isBreak(f) {
  if (!f || !f.step) return false;
  return S.stepBps.has(f.step.id) || (f.step.line && S.lineBps.has(f.step.line));
}
function countText(s) {
  const out = s.panels[s.panels.length - 1];
  if (!out) return "";
  if (s.kind === "filter" && out.marks) return `${out.marks.filter((m) => m === true).length}/${out.total}`;
  return `${out.total}${out.overflow ? "+" : ""} 列`;
}
function renderPlan() {
  const plan = $("#plan");
  if (!S.trace) {
    plan.innerHTML = '<div class="plan-empty">執行後，這裡會依照 SQL 實際的執行順序列出每個步驟。<br><br>點步驟前的圓點可以設定斷點。</div>';
    return;
  }
  plan.innerHTML = "";
  const multi = S.trace.statements.length > 1;
  let lastStmt = -1;
  let lastPipe = null;
  let pipeBox = null;
  S.flat.forEach((f, i) => {
    if (multi && f.stmtIndex !== lastStmt) {
      const st = S.trace.statements[f.stmtIndex];
      plan.appendChild(el("div", { class: "stmt-head" }, `敘述 ${f.stmtIndex + 1}${st ? `（第 ${st.line} 行）` : ""}`));
      lastStmt = f.stmtIndex;
    }
    if (f.pipe && f.pipe !== lastPipe) {
      pipeBox = el("div", { class: "pipe" });
      pipeBox.style.marginLeft = `${f.pipe.depth * 10}px`;
      pipeBox.appendChild(el("div", { class: "pipe-title" }, esc(f.pipe.title)));
      for (const n of f.pipe.notes || []) pipeBox.appendChild(el("div", { class: "pipe-note" }, "⚠ " + esc(n)));
      plan.appendChild(pipeBox);
      lastPipe = f.pipe;
    }
    let row;
    if (f.error) {
      row = el("div", { class: "step err" + (i === S.cur ? " current" : "") },
        `<span class="chip">ERROR</span><span class="stitle">${esc(f.error.split("\n")[0])}</span>`);
      plan.appendChild(row);
    } else {
      const s = f.step;
      const cls = ["step", isBreak(f) ? "bp" : "", i === S.cur ? "current" : "", i < S.cur ? "done" : ""].join(" ");
      row = el("div", { class: cls, title: s.title },
        `<span class="sbp" title="設定 / 取消斷點"></span><span class="chip k-${s.kind}">${esc(s.clause)}</span>` +
        `<span class="stitle">${esc(s.title)}</span><span class="scount">${countText(s)}</span>` +
        (s.line ? `<span class="sline">L${s.line}</span>` : ""));
      row.querySelector(".sbp").addEventListener("click", (e) => {
        e.stopPropagation();
        if (S.stepBps.has(s.id)) S.stepBps.delete(s.id); else S.stepBps.add(s.id);
        renderPlan();
        save();
      });
      (pipeBox || plan).appendChild(row);
    }
    row.addEventListener("click", () => { pause(); goTo(i, true); });
    if (i === S.cur) row.scrollIntoView({ block: "nearest" });
  });
}

// ---------------------------------------------------------------------------
// stage: build the DOM for a step once, then update classes as it animates
// ---------------------------------------------------------------------------
function buildPanel(p, opts) {
  const box = el("div", { class: "pnl " + (p.role || "output") });
  const more = p.total > p.rows.length ? `，顯示前 ${p.rows.length} 列` : "";
  const extra = p.overflow ? "（超過上限，只取前 100,000 列）" : "";
  box.appendChild(el("div", { class: "pnl-head" },
    `<span>${esc(p.title)}</span><span class="cnt">共 ${p.total}${p.overflow ? "+" : ""} 列${more}${extra}</span>`));
  const wrap = el("div", { class: "pnl-wrap" });
  const hl = new Set(p.highlight || []);
  const hasMarks = Array.isArray(p.marks);
  let head = '<tr><th class="rownum">#</th>';
  if (hasMarks) head += `<th class="mark">${esc(opts.markLabel || "條件")}</th>`;
  p.columns.forEach((c, i) => { head += `<th class="${hl.has(i) ? "hl-col" : ""}">${esc(c)}</th>`; });
  head += "</tr>";
  const rowsHtml = p.rows.map((r, ri) => {
    let tr = `<td class="rownum">${ri + 1}</td>`;
    if (hasMarks) tr += '<td class="mark"></td>';
    const changed = new Set((p.changed && p.changed[ri]) || []);
    r.forEach((v, ci) => {
      const f = fmtCell(v);
      const cls = [f.num ? "n" : "", hl.has(ci) ? "hl-col" : "", changed.has(ci) ? "cell-changed" : ""].filter(Boolean).join(" ");
      tr += `<td class="${cls}"${f.title ? ` title="${esc(f.title)}"` : ""}>${f.html}</td>`;
    });
    let cls = "";
    if (p.groups && p.groups[ri] !== null && p.groups[ri] !== undefined) cls += " grp";
    if (p.status) cls += " st-" + p.status[ri];
    const style = p.groups ? ` style="--gc:${groupColor(p.groups[ri])}"` : "";
    return `<tr class="${cls.trim()}"${style}>${tr}</tr>`;
  }).join("");
  wrap.innerHTML = `<table class="grid"><thead>${head}</thead><tbody>${rowsHtml}</tbody></table>`;
  box.appendChild(wrap);
  const trs = [...wrap.querySelectorAll("tbody tr")];
  const byId = new Map();
  (p.ids || []).forEach((id, i) => { if (!byId.has(id)) byId.set(id, []); byId.get(id).push(i); });
  return { el: box, wrap, trs, byId, p };
}

function units(step) {
  if (!step || !step.panels.length) return 1;
  if (step.anim === "diff") return 1;
  const out = step.panels[step.panels.length - 1];
  return Math.max(1, out.rows.length);
}

function renderStage() {
  const stage = $("#stage");
  stage.innerHTML = "";
  S.view = null;
  const f = S.flat[S.cur];
  if (!f) {
    stage.appendChild(welcome());
    return;
  }
  if (f.error) {
    stage.appendChild(el("div", { class: "stage-head" },
      `<div class="crumb">敘述 ${f.stmtIndex + 1}${f.line ? `・第 ${f.line} 行` : ""}</div><div class="stage-title">執行錯誤</div>`));
    stage.appendChild(el("div", { class: "errcard" }, esc(f.error)));
    return;
  }
  const s = f.step;
  const head = el("div", { class: "stage-head" });
  const stmtLabel = S.trace.statements.length > 1 ? `敘述 ${f.stmtIndex + 1} › ` : "";
  head.innerHTML =
    `<div class="crumb">${esc(stmtLabel + f.pipe.title)} › 第 ${f.pipe.steps.indexOf(s) + 1} / ${f.pipe.steps.length} 步${s.line ? `・第 ${s.line} 行` : ""}</div>` +
    `<div class="stage-title"><span class="chip k-${s.kind}">${esc(s.clause)}</span><span>${esc(s.title)}</span></div>` +
    `<p class="explain">${esc(s.explain)}</p>`;
  for (const n of s.notes || []) head.appendChild(el("div", { class: "note" }, "⚠ " + esc(n)));
  const bpBanner = el("div", { class: "bp-banner" });
  bpBanner.hidden = true;
  head.appendChild(bpBanner);
  const meta = el("div", { class: "meta-row" });
  const prog = el("div", { class: "progress" }, '<div class="bar"><i></i></div><span class="ptext"></span>');
  meta.appendChild(prog);
  const legend = {
    filter: '<span style="--c:var(--pass)">TRUE 保留</span><span style="--c:var(--fail)">FALSE 丟棄</span>' +
      '<span style="--c:var(--unk)">NULL（未知）丟棄</span>',
    match: '<span style="--c:var(--just-bg)">目前產生的列</span><span style="--c:var(--link-bg)">它的來源列</span>',
    aggregate: '<span style="--c:var(--just-bg)">目前產生的組</span><span style="--c:var(--link-bg)">這一組的成員列</span>',
    diff: '<span style="--c:var(--pass-bg)">新增</span><span style="--c:var(--unk-bg)">修改</span><span style="--c:var(--fail-bg)">刪除</span>',
  }[s.anim];
  if (legend) meta.appendChild(el("div", { class: "legend" }, legend));
  if (s.sql) {
    meta.appendChild(el("details", { class: "sql" }, `<summary>查看這一步實際執行的 SQL</summary><pre>${esc(s.sql)}</pre>`));
  }
  head.appendChild(meta);
  stage.appendChild(head);

  const panels = el("div", { class: "panels" + (s.panels.length >= 3 ? " three" : "") });
  const markLabel = { WHERE: "WHERE", HAVING: "HAVING", QUALIFY: "QUALIFY", DISTINCT: "保留?", LIMIT: "範圍內?" }[s.clause];
  const views = s.panels.map((p) => {
    const v = buildPanel(p, { markLabel });
    panels.appendChild(v.el);
    return v;
  });
  stage.appendChild(panels);
  const summary = el("div", { class: "summary" });
  stage.appendChild(summary);
  S.view = { step: s, views, out: views[views.length - 1], prog, bpBanner, summary, U: units(s) };
  wireHover(S.view);
}

function welcome() {
  return el("div", { class: "welcome" }, `
    <h1>SQL Stepper</h1>
    <p>把一段 SQL 拆成它<b>實際的執行順序</b>，一步一步、一列一列地看中間結果。</p>
    <ol>
      <li>在上方編輯器寫 SQL，或從右上角「載入範例查詢」挑一個。</li>
      <li>按 <b>▶ 執行</b>（Ctrl+Enter），動畫會從 FROM 開始播放。</li>
      <li>點行號設定斷點，播放到那一行的步驟時會暫停；再用 <b>↧ 下一列</b> 逐列前進。</li>
    </ol>
    <p>左側可以匯入自己的 CSV / Parquet / JSON / Excel / DuckDB / SQLite 檔案。</p>`);
}

function clearMarks(v) {
  for (const pv of v.views) for (const tr of pv.trs) tr.classList.remove("just", "hl-link", "scan", "dim", "pending");
}
function ensureVisible(wrap, tr) {
  if (!tr || !wrap) return;
  const top = tr.offsetTop - 30;
  const bottom = tr.offsetTop + tr.offsetHeight;
  if (top < wrap.scrollTop) wrap.scrollTop = top;
  else if (bottom > wrap.scrollTop + wrap.clientHeight) wrap.scrollTop = bottom - wrap.clientHeight + 4;
}
function highlightLinks(v, outIndex, scroll) {
  const links = v.step.links[outIndex] || [];
  for (const [pi, id] of links) {
    const pv = v.views[pi];
    if (!pv) continue;
    const idxs = pv.byId.get(id) || [];
    idxs.forEach((i, k) => {
      pv.trs[i].classList.add("hl-link");
      if (scroll && k === 0) ensureVisible(pv.wrap, pv.trs[i]);
    });
  }
}
function markText(m) { return m === true ? "✓ TRUE" : m === false ? "✗ FALSE" : "? NULL"; }
function markLabelText(clause, m) {
  if (clause === "DISTINCT") return m ? "✓ 保留" : "✗ 重複";
  if (clause === "LIMIT") return m ? "✓ 保留" : "✗ 略過";
  return markText(m);
}

function applyProgress() {
  const v = S.view;
  if (!v) return;
  const s = v.step;
  const U = v.U;
  const p = Math.max(0, Math.min(S.prog, U));
  const complete = p >= U;
  clearMarks(v);
  const out = v.out;
  const anim = s.anim;

  if (anim === "reveal" || anim === "match" || anim === "group" || anim === "columns") {
    out.trs.forEach((tr, i) => {
      if (anim === "columns" || anim === "group") {
        tr.classList.toggle("tint", anim === "group" ? i < p : true);
        if (anim === "columns") {
          tr.querySelectorAll("td.hl-col").forEach((td) => td.classList.toggle("masked", i >= p));
        }
      } else if (i >= p) {
        tr.classList.add("pending");
      }
      if (i === p - 1 && !complete) tr.classList.add("just");
    });
    if (anim === "match" && p > 0 && !complete) highlightLinks(v, p - 1, true);
    if (!complete && p > 0) ensureVisible(out.wrap, out.trs[p - 1]);
  } else if (anim === "filter") {
    const marks = out.p.marks || [];
    out.trs.forEach((tr, i) => {
      const td = tr.querySelector("td.mark");
      tr.classList.remove("mk-pass", "mk-fail", "mk-unk", "dropped");
      if (i < p) {
        const m = marks[i];
        tr.classList.add(m === true ? "mk-pass" : m === false ? "mk-fail" : "mk-unk");
        if (td) td.textContent = markLabelText(s.clause, m);
        if (complete && m !== true) tr.classList.add("dropped");
      } else if (td) {
        td.textContent = "";
      }
      if (i === p - 1 && !complete) tr.classList.add("scan");
    });
    if (!complete && p > 0) ensureVisible(out.wrap, out.trs[p - 1]);
  } else if (anim === "aggregate") {
    const inp = v.views.length > 1 ? v.views[0] : null;
    if (inp) inp.trs.forEach((tr) => tr.classList.add("tint"));
    out.trs.forEach((tr, i) => {
      tr.classList.add("tint");
      if (i >= p) tr.classList.add("pending");
      if (i === p - 1 && !complete) tr.classList.add("just");
    });
    if (inp && p > 0 && !complete) {
      const g = out.p.groups ? out.p.groups[p - 1] : null;
      let first = null;
      inp.trs.forEach((tr, i) => {
        const same = inp.p.groups && inp.p.groups[i] === g;
        tr.classList.add(same ? "hl-link" : "dim");
        if (same && !first) first = tr;
      });
      ensureVisible(inp.wrap, first);
    }
  }
  // progress + banner
  v.prog.querySelector(".bar > i").style.width = `${(100 * p) / U}%`;
  const unitName = anim === "aggregate" ? "組" : "列";
  v.prog.querySelector(".ptext").textContent = anim === "diff" ? "" : complete
    ? `完成（${U} ${unitName}）` : `處理中：第 ${p} / ${U} ${unitName}`;
  if (complete && anim === "filter") {
    const marks = out.p.marks || [];
    const kept = marks.filter((m) => m === true).length;
    const nul = marks.filter((m) => m === null).length;
    v.summary.textContent = `結果：${marks.length} 列 → 保留 ${kept} 列` + (nul ? `（其中 ${nul} 列因為 NULL 而被丟棄）` : "");
  } else {
    v.summary.textContent = "";
  }
}

function wireHover(v) {
  // When paused, hovering a row shows where it came from (or what it produced).
  const s = v.step;
  if (s.anim !== "match" && s.anim !== "aggregate") return;
  const outIdx = v.views.length - 1;
  v.out.trs.forEach((tr, i) => {
    tr.addEventListener("mouseenter", () => {
      if (S.playing || S.prog < v.U) return;
      clearMarks(v);
      tr.classList.add("just");
      if (s.anim === "match") highlightLinks(v, i, true);
      else if (v.views.length > 1) {
        const g = v.out.p.groups ? v.out.p.groups[i] : null;
        v.views[0].trs.forEach((t, k) => t.classList.add(v.views[0].p.groups[k] === g ? "hl-link" : "dim"));
      }
    });
    tr.addEventListener("mouseleave", () => { if (!S.playing && S.prog >= v.U) clearMarks(v); });
  });
  if (s.anim === "match") {
    const reverse = new Map();  // "panel:id" -> output row indexes
    s.links.forEach((link, oi) => link.forEach(([pi, id]) => {
      const k = pi + ":" + id;
      if (!reverse.has(k)) reverse.set(k, []);
      reverse.get(k).push(oi);
    }));
    v.views.slice(0, outIdx).forEach((pv, pi) => {
      pv.trs.forEach((tr, i) => {
        tr.addEventListener("mouseenter", () => {
          if (S.playing || S.prog < v.U) return;
          clearMarks(v);
          tr.classList.add("hl-link");
          const id = pv.p.ids ? pv.p.ids[i] : null;
          (reverse.get(pi + ":" + id) || []).forEach((oi, k) => {
            v.out.trs[oi].classList.add("just");
            if (k === 0) ensureVisible(v.out.wrap, v.out.trs[oi]);
          });
        });
        tr.addEventListener("mouseleave", () => { if (!S.playing && S.prog >= v.U) clearMarks(v); });
      });
    });
  }
}

// ---------------------------------------------------------------------------
// playback
// ---------------------------------------------------------------------------
function delay() {
  const base = DELAYS[Number($("#speed").value) - 1];
  const U = S.view ? S.view.U : 1;
  return Math.max(8, Math.min(base, 9000 / U));  // long steps speed up so each lasts at most ~9s
}
function clearTimer() { if (S.timer) { clearTimeout(S.timer); S.timer = null; } }
function updateButtons() {
  const has = S.flat.length > 0;
  $("#play").disabled = !has || S.stale;
  $("#next").disabled = !has || S.stale;
  $("#row").disabled = !has || S.stale;
  $("#prev").disabled = !has || S.stale || S.cur <= 0;
  $("#play").innerHTML = S.playing ? "⏸ 暫停<kbd>F5</kbd>" : "⏵ 繼續<kbd>F5</kbd>";
}
function goTo(i, complete) {
  clearTimer();
  S.cur = i;
  renderStage();
  S.prog = complete ? (S.view ? S.view.U : 1) : 0;
  applyProgress();
  renderPlan();
  renderGutter();
  revealLine(currentLine());
  updateButtons();
}
function describe(f) {
  if (!f) return "";
  if (f.error) return "錯誤";
  return `${f.step.clause}${f.step.line ? `（第 ${f.step.line} 行）` : ""}`;
}
function enterStep(i) {
  // Called while playing: pause *before* running a step that has a breakpoint.
  goTo(i, false);
  const f = S.flat[i];
  if (f && f.error) {
    stop("執行錯誤", "err");
    return;
  }
  if (isBreak(f)) {
    S.playing = false;
    S.stepOnly = false;
    S.view.bpBanner.hidden = false;
    S.view.bpBanner.textContent = "⏸ 斷點：還沒執行這一步。按 ↧ 下一列 逐列前進，或 ⏵ 繼續。";
    setStatus(`⏸ 停在斷點：${describe(f)}`, "warn");
    updateButtons();
    return;
  }
  schedule();
}
function schedule() {
  clearTimer();
  if (!S.playing) return;
  S.timer = setTimeout(tick, $("#animate").checked ? delay() : Math.max(250, DELAYS[Number($("#speed").value) - 1] * 2));
}
function tick() {
  if (!S.playing || !S.view) return;
  if (!$("#animate").checked) S.prog = S.view.U;
  if (S.prog < S.view.U) {
    S.prog += 1;
    applyProgress();
    schedule();
    return;
  }
  // step finished
  if (S.stepOnly) {
    stop(`已完成：${describe(S.flat[S.cur])}`);
    return;
  }
  if (S.cur >= S.flat.length - 1) {
    stop("✔ 執行完畢", "go");
    renderPlan();
    return;
  }
  S.timer = setTimeout(() => enterStep(S.cur + 1), Math.min(700, delay() * 3));
}
function play() {
  if (S.stale || !S.flat.length) return run();
  if (S.playing) return pause();
  if (S.cur < 0) S.cur = 0;
  if (S.cur >= S.flat.length - 1 && S.view && S.prog >= S.view.U) {
    goTo(0, false);  // at the very end: replay from the beginning
  }
  S.playing = true;
  S.stepOnly = false;
  if (S.view) S.view.bpBanner.hidden = true;
  setStatus("▶ 播放中…", "go");
  updateButtons();
  schedule();
}
function pause() {
  if (!S.playing) return;
  S.playing = false;
  S.stepOnly = false;
  clearTimer();
  setStatus(`⏸ 已暫停：${describe(S.flat[S.cur])}`);
  updateButtons();
}
function stop(msg, cls = "") {
  S.playing = false;
  S.stepOnly = false;
  clearTimer();
  setStatus(msg, cls);
  updateButtons();
}
function nextStep() {
  if (S.stale || !S.flat.length) return;
  pause();
  if (S.view && S.prog < S.view.U) {
    S.playing = true;  // finish the current step's animation, then stop
  } else if (S.cur < S.flat.length - 1) {
    goTo(S.cur + 1, false);
    S.playing = true;
  } else {
    return stop("✔ 已經是最後一步", "go");
  }
  S.stepOnly = true;
  if (S.view) S.view.bpBanner.hidden = true;
  setStatus(`▶ ${describe(S.flat[S.cur])}`, "go");
  updateButtons();
  schedule();
}
function nextRow() {
  if (S.stale || !S.flat.length) return;
  pause();
  if (S.view && S.prog < S.view.U) {
    S.prog += 1;
    applyProgress();
  } else if (S.cur < S.flat.length - 1) {
    goTo(S.cur + 1, false);
    S.prog = S.view && S.view.U > 0 ? 1 : 0;
    applyProgress();
  } else {
    return stop("✔ 已經是最後一步", "go");
  }
  if (S.view) S.view.bpBanner.hidden = true;
  setStatus(`⏸ ${describe(S.flat[S.cur])}：第 ${S.prog} / ${S.view ? S.view.U : 1} 列`);
}
function prevStep() {
  if (S.stale || S.cur <= 0) return;
  pause();
  goTo(S.cur - 1, true);
}

async function run() {
  pause();
  const sql = ed.value;
  if (!sql.trim()) {
    toast("請先輸入 SQL", true);
    return;
  }
  setStatus("計算中…");
  $("#run").disabled = true;
  try {
    const trace = await api("/api/trace", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sql, sandbox: $("#sandbox").checked }),
    });
    S.trace = trace;
    S.flat = flatten(trace);
    S.stale = false;
    if (!$("#sandbox").checked) refreshState().catch(() => {});
    if (!S.flat.length) {
      S.cur = -1;
      renderPlan();
      renderStage();
      setStatus("沒有可以執行的敘述", "warn");
      return;
    }
    S.playing = true;
    S.stepOnly = false;
    setStatus("▶ 播放中…", "go");
    enterStep(0);
    if (trace.error && S.flat.length === 1) stop("執行錯誤", "err");
  } catch (err) {
    toast(err.message, true);
    setStatus("執行失敗", "err");
  } finally {
    $("#run").disabled = false;
    updateButtons();
  }
}

// ---------------------------------------------------------------------------
// wiring
// ---------------------------------------------------------------------------
$("#run").addEventListener("click", run);
$("#play").addEventListener("click", play);
$("#next").addEventListener("click", nextStep);
$("#row").addEventListener("click", nextRow);
$("#prev").addEventListener("click", prevStep);
$("#clear-bp").addEventListener("click", () => {
  S.lineBps.clear();
  S.stepBps.clear();
  renderGutter();
  renderPlan();
  save();
});
$("#speed").addEventListener("input", save);
$("#animate").addEventListener("change", save);
$("#sandbox").addEventListener("change", save);
$("#help-btn").addEventListener("click", () => $("#help").showModal());
$("#side-toggle").addEventListener("click", () => $("#sidebar").classList.toggle("show"));
$("#theme").addEventListener("click", () => {
  const root = document.documentElement;
  const dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  root.dataset.theme = dark ? "light" : "dark";
  save();
});
$("#examples").addEventListener("change", (e) => {
  const ex = S.examples[Number(e.target.value)];
  e.target.value = "";
  if (!ex) return;
  if (ed.value.trim() && !confirm("用範例取代目前的 SQL？")) return;
  ed.value = ex.sql;
  S.lineBps.clear();
  ed.dispatchEvent(new Event("input"));
  run();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); run(); }
  else if (e.key === "F5") { e.preventDefault(); play(); }
  else if (e.key === "F10") { e.preventDefault(); nextStep(); }
  else if (e.key === "F8") { e.preventDefault(); nextRow(); }
  else if (e.key === "F9") {
    e.preventDefault();
    toggleLineBp(ed.value.slice(0, ed.selectionStart).split("\n").length);
  }
});
(function resizer() {
  const bar = $("#resizer");
  let startY = 0;
  let startH = 0;
  const move = (e) => {
    const h = Math.max(120, Math.min(window.innerHeight - 200, startH + e.clientY - startY));
    document.documentElement.style.setProperty("--editor-h", h + "px");
    syncScroll();
  };
  const up = () => {
    document.removeEventListener("pointermove", move);
    document.removeEventListener("pointerup", up);
    save();
  };
  bar.addEventListener("pointerdown", (e) => {
    startY = e.clientY;
    startH = $("#editor-pane").getBoundingClientRect().height;
    document.addEventListener("pointermove", move);
    document.addEventListener("pointerup", up);
  });
})();

(async function init() {
  const saved = load();
  if (saved.theme) document.documentElement.dataset.theme = saved.theme;
  if (saved.editorH) document.documentElement.style.setProperty("--editor-h", saved.editorH);
  if (saved.speed) $("#speed").value = saved.speed;
  if (typeof saved.animate === "boolean") $("#animate").checked = saved.animate;
  if (typeof saved.sandbox === "boolean") $("#sandbox").checked = saved.sandbox;
  (saved.lineBps || []).forEach((l) => S.lineBps.add(l));
  (saved.stepBps || []).forEach((l) => S.stepBps.add(l));
  try {
    await refreshState();
    const sel = $("#examples");
    S.examples.forEach((ex, i) => sel.appendChild(el("option", { value: String(i) }, esc(ex.title))));
    ed.value = saved.sql || (S.examples[0] ? S.examples[0].sql : "SELECT 1;");
  } catch (err) {
    toast("無法連線到伺服器：" + err.message, true);
  }
  highlight();
  renderPlan();
  renderStage();
  updateButtons();
})();
