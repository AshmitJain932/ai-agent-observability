/* AOBS dashboard: a dependency-free single-page app over the read-only /api.
 *
 * Every value from the API is inserted with textContent (via h()), never as HTML, so a trace
 * cannot inject markup. Routes: #/<dataset>/overview | runs | run/<trace_id> | compare.
 */
"use strict";

const TOKEN_KEY = "aobs-dashboard-token";
const state = { datasets: [], dataset: null };

// ---------------------------------------------------------------- DOM helpers

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "style") {
      // set through the CSSOM: inline style attributes are blocked by the Content-Security-Policy
      for (const decl of String(value).split(";")) {
        const at = decl.indexOf(":");
        if (at > 0) el.style.setProperty(decl.slice(0, at).trim(), decl.slice(at + 1).trim());
      }
    } else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else el.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

function show(...nodes) {
  const main = document.getElementById("main");
  main.replaceChildren(...nodes.flat(Infinity).filter((n) => n !== null && n !== undefined && n !== false));
}

const loading = (what) => h("p", { class: "state state-loading", role: "status" }, `Loading ${what}…`);
const empty = (title, text, ...extra) =>
  h("div", { class: "state state-empty" }, h("strong", {}, title), text, ...extra);

function errorState(error, retry) {
  return h(
    "div",
    { class: "state state-error", role: "alert" },
    h("strong", {}, error.status ? `Request failed (${error.status})` : "Could not reach the dashboard server"),
    error.message,
    retry ? h("div", {}, h("button", { onclick: retry, style: "margin-top:10px" }, "Retry")) : null,
  );
}

// ---------------------------------------------------------------- formatting

function fmtMs(value) {
  if (value === null || value === undefined) return "—";
  if (value >= 1000) return `${(value / 1000).toFixed(2)} s`;
  return `${value.toFixed(1)} ms`;
}
const fmtInt = (n) => (n === null || n === undefined ? "—" : Number(n).toLocaleString("en-US"));
const fmtPct = (p) => (p === null || p === undefined ? "—" : `${(p * 100).toFixed(p < 0.1 && p > 0 ? 1 : 0)}%`);
function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toISOString().replace("T", " ").replace(/\.\d+Z$/, "Z");
}
const short = (id) => (id ? id.slice(0, 12) : "");

const OUTCOME = {
  succeeded: ["good", "✓", "succeeded"],
  failed: ["critical", "✗", "failed"],
  cancelled: ["warning", "■", "cancelled"],
  unfinished: ["warning", "…", "unfinished"],
};
function statusBadge(status, outcome) {
  const [cls, icon, label] = OUTCOME[outcome] || ["", "•", status];
  const text = status && status !== label && status !== "ok" ? `${label} (${status.replaceAll("_", " ")})` : label;
  return h("span", { class: `badge ${cls}` }, h("span", { "aria-hidden": "true" }, icon), text);
}
const SEVERITY = { error: ["critical", "✗", "error"], warning: ["warning", "!", "warning"], info: ["info", "i", "info"] };
const severityBadge = (sev) => {
  const [cls, icon, label] = SEVERITY[sev] || ["", "•", sev];
  return h("span", { class: `badge ${cls}` }, h("span", { "aria-hidden": "true" }, icon), label);
};
const VERDICT = {
  likely_regression: ["critical", "✗", "likely regression", "error"],
  weak_signal: ["warning", "?", "weak signal", "warning"],
  minor_change: ["", "~", "minor change", "info"],
  possible_improvement: ["good", "✓", "possible improvement", "good"],
  no_change: ["", "=", "no change", "info"],
  insufficient_data: ["dashed", "…", "insufficient data", "info"],
};
const verdictBadge = (v) => {
  const [cls, icon, label] = VERDICT[v] || ["", "•", v];
  return h("span", { class: `badge ${cls}` }, h("span", { "aria-hidden": "true" }, icon), label);
};
function findingCounts(f) {
  const parts = [];
  if (f.error) parts.push(h("span", { class: "badge critical" }, `${f.error} error${f.error > 1 ? "s" : ""}`));
  if (f.warning) parts.push(h("span", { class: "badge warning" }, `${f.warning} warning${f.warning > 1 ? "s" : ""}`));
  if (f.info) parts.push(h("span", { class: "badge info" }, `${f.info} info`));
  return parts.length ? parts : h("span", { class: "muted" }, "none");
}

// ---------------------------------------------------------------- API

class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

async function api(path, params) {
  const url = new URL(path, window.location.origin);
  for (const [key, value] of Object.entries(params || {})) {
    for (const v of [].concat(value)) if (v !== undefined && v !== null && v !== "") url.searchParams.append(key, v);
  }
  const headers = { Accept: "application/json" };
  const token = sessionStorage.getItem(TOKEN_KEY);
  if (token) headers.Authorization = `Bearer ${token}`;
  let response;
  try {
    response = await fetch(url, { headers });
  } catch (err) {
    throw new ApiError(0, "The dashboard server did not answer. Is `aobs-server dashboard` still running?");
  }
  let body = null;
  try {
    body = await response.json();
  } catch (err) {
    body = null;
  }
  if (!response.ok) throw new ApiError(response.status, (body && body.detail) || response.statusText);
  return body;
}

// ---------------------------------------------------------------- routing

function parseRoute() {
  const raw = window.location.hash.replace(/^#\/?/, "");
  const [pathPart, query = ""] = raw.split("?");
  const parts = pathPart.split("/").filter(Boolean).map(decodeURIComponent);
  return { dataset: parts[0], view: parts[1] || "overview", arg: parts[2], params: new URLSearchParams(query) };
}

function go(view, arg, params) {
  const query = params && [...params].length ? `?${new URLSearchParams(params)}` : "";
  window.location.hash = `#/${encodeURIComponent(state.dataset.id)}/${view}${arg ? `/${encodeURIComponent(arg)}` : ""}${query}`;
}
const link = (view, arg, text, params) => {
  const query = params ? `?${new URLSearchParams(params)}` : "";
  return h("a", { href: `#/${encodeURIComponent(state.dataset.id)}/${view}${arg ? `/${arg}` : ""}${query}` }, text);
};

async function route() {
  const r = parseRoute();
  const dataset = state.datasets.find((d) => d.id === r.dataset) || state.datasets[0];
  if (!dataset) {
    show(empty("No datasets available", "Start the dashboard with sample data or with --data-dir pointing at trace files."));
    return;
  }
  if (dataset.id !== r.dataset) {
    state.dataset = dataset;
    go("overview");
    return;
  }
  state.dataset = dataset;
  document.getElementById("dataset").value = dataset.id;
  const badge = document.getElementById("source-badge");
  badge.hidden = false;
  badge.className = `source-badge ${dataset.kind}`;
  badge.textContent = dataset.kind === "sample" ? "Sample data" : "Local files";
  const note = document.getElementById("source-note");
  note.hidden = false;
  note.textContent =
    dataset.kind === "sample"
      ? `Sample recordings shipped with the repository, not live production traffic. ${dataset.description}`
      : `${dataset.description} These are files on disk, not live ingestion.`;
  for (const tab of document.querySelectorAll(".tabs a")) {
    const active = tab.dataset.tab === r.view || (tab.dataset.tab === "runs" && r.view === "run");
    tab.setAttribute("href", `#/${encodeURIComponent(dataset.id)}/${tab.dataset.tab}`);
    if (active) tab.setAttribute("aria-current", "page");
    else tab.removeAttribute("aria-current");
  }
  const views = { overview: viewOverview, runs: viewRuns, run: viewRun, compare: viewCompare };
  await (views[r.view] || viewOverview)(r);
}

// wrap a view so 401s show the token form and other failures an error state
async function guarded(what, render) {
  show(loading(what));
  try {
    await render();
  } catch (err) {
    if (err.status === 401) show(tokenForm());
    else show(errorState(err, () => route()));
  }
}

function tokenForm() {
  const input = h("input", { type: "password", id: "token-input", "aria-label": "Dashboard token", autocomplete: "off" });
  const save = (event) => {
    event.preventDefault();
    sessionStorage.setItem(TOKEN_KEY, input.value.trim());
    route();
  };
  return h(
    "div",
    { class: "state" },
    h("strong", {}, "This dataset needs the dashboard token"),
    "It was printed in the terminal when the dashboard started (or open the URL that includes #token=…).",
    h("form", { class: "token-form", onsubmit: save }, input, h("button", { class: "primary", type: "submit" }, "Open")),
  );
}

// ---------------------------------------------------------------- overview

function tile(label, value, note) {
  return h("div", { class: "tile" }, h("div", { class: "tile-label" }, label), h("div", { class: "tile-value" }, value),
    note ? h("div", { class: "tile-note" }, note) : null);
}

function runsTable(runs) {
  return h(
    "div",
    { class: "table-wrap" },
    h(
      "table",
      {},
      h("thead", {}, h("tr", {}, ["Status", "Run", "Agent", "File", "Started (UTC)"].map((t) => h("th", {}, t)),
        h("th", { class: "num" }, "Duration"), h("th", { class: "num" }, "Spans"), h("th", { class: "num" }, "Failed"),
        h("th", {}, "Findings"))),
      h(
        "tbody",
        {},
        runs.map((run) =>
          h(
            "tr",
            { class: "clickable", "data-trace": run.trace_id },
            h("td", {}, statusBadge(run.status, run.outcome)),
            h("td", {}, link("run", run.trace_id, run.run || "(no root span)"), h("div", { class: "muted mono" }, short(run.trace_id))),
            h("td", {}, run.agent || "—"),
            h("td", { class: "mono" }, run.file),
            h("td", { class: "mono" }, fmtTime(run.started_at)),
            h("td", { class: "num" }, fmtMs(run.duration_ms)),
            h("td", { class: "num" }, run.span_count),
            h("td", { class: "num" }, run.error_count ? h("span", { class: "badge critical" }, `✗ ${run.error_count}`) : "0"),
            h("td", {}, findingCounts(run.findings)),
          ),
        ),
      ),
    ),
  );
}

function problemsList(problems) {
  if (!problems || !problems.length) return null;
  return h("div", { class: "notice warn" }, h("strong", {}, `Input problems (${problems.length})`),
    h("ul", {}, problems.slice(0, 10).map((p) => h("li", { class: "mono" }, p))));
}

async function viewOverview() {
  await guarded("overview", async () => {
    const data = await api(`/api/datasets/${state.dataset.id}/overview`);
    if (!data.runs) {
      show(h("h1", {}, "Overview"), problemsList(data.input_problems),
        empty("No runs in this dataset yet", state.dataset.kind === "local"
          ? "Write traces with aobs.export.JsonlFileExporter into this directory, then reload."
          : "The sample files are missing; re-create them with the scripts in examples/diagnose/."));
      return;
    }
    const tin = data.tokens.input_tokens;
    const tout = data.tokens.output_tokens;
    const tokenTile = (label, t) => (t.total === null
      ? tile(label, "not reported", t.runs_with_model_calls ? "no run reported usage on every model call" : "no model calls")
      : tile(label, fmtInt(t.total), `sum over ${t.runs_counted} of ${data.runs} runs with complete usage`));
    const costTile = data.cost.total === null
      ? tile("Cost (provider-reported)", "not reported", data.cost.note)
      : tile("Cost (provider-reported)", h("span", {}, data.cost.total, h("small", { class: "unit" }, ` ${data.cost.currency}`)),
        `sum over ${data.cost.runs_counted} of ${data.runs} runs reporting cost on every call; never estimated`);
    show(
      h("h1", {}, "Overview"),
      h("p", { class: "subtle" }, state.dataset.title),
      problemsList(data.input_problems),
      h("div", { class: "tiles", id: "kpis" },
        tile("Total runs", fmtInt(data.runs)),
        tile("Succeeded", fmtInt(data.succeeded), "status ok, or ok with errors in sub-steps"),
        tile("Failed", fmtInt(data.failed), data.unfinished || data.cancelled
          ? `${data.unfinished} unfinished, ${data.cancelled} cancelled (not counted)` : null),
        tile("Failure rate", fmtPct(data.failure_rate), `of ${data.finished} finished runs`),
        tile("Median run duration", fmtMs(data.median_duration_ms), `over ${data.durations_counted} finished runs`),
        tokenTile("Input tokens", tin),
        tokenTile("Output tokens", tout),
        costTile),
      h("h2", {}, "Recent runs"),
      runsTable(data.recent),
      h("p", {}, link("runs", null, `All ${data.runs} runs →`)),
    );
  });
}

// ---------------------------------------------------------------- runs explorer

async function viewRuns(r) {
  await guarded("runs", async () => {
    const status = r.params.get("status") || "";
    const task = r.params.get("task") || "";
    const [list, overview] = await Promise.all([
      api(`/api/datasets/${state.dataset.id}/runs`, { status, task }),
      api(`/api/datasets/${state.dataset.id}/overview`),
    ]);
    const update = (key, value) => {
      const params = new URLSearchParams(r.params);
      if (value) params.set(key, value);
      else params.delete(key);
      go("runs", null, params);
    };
    const statusSelect = h("select", { id: "filter-status", onchange: (e) => update("status", e.target.value) },
      h("option", { value: "" }, "All"),
      ["succeeded", "failed", "cancelled", "unfinished"].map((s) => h("option", { value: s, selected: s === status }, s)));
    const taskSelect = h("select", { id: "filter-task", onchange: (e) => update("task", e.target.value) },
      h("option", { value: "" }, "All tasks"),
      overview.tasks.map((t) => h("option", { value: t, selected: t === task }, t)));
    const body = list.runs.length
      ? runsTable(list.runs)
      : list.total
        ? empty("No runs match these filters", "", h("div", {}, h("button", { onclick: () => go("runs") }, "Clear filters")))
        : empty("No runs in this dataset yet", "Add JSONL trace files and reload.");
    show(
      h("h1", {}, "Runs"),
      h("p", { class: "subtle" }, `${list.runs.length} of ${list.total} runs`),
      h("div", { class: "filters" }, h("label", {}, "Status", statusSelect), h("label", {}, "Task", taskSelect)),
      body,
    );
  });
}

// ---------------------------------------------------------------- run details

function findingCard(f) {
  return h(
    "article",
    { class: `finding ${f.severity}` },
    h("div", {}, severityBadge(f.severity), " ", h("span", { class: "rule" }, f.rule)),
    h("h3", {}, f.title),
    h("dl", {},
      h("dt", {}, "Evidence"), h("dd", {}, h("ul", {}, f.evidence.map((e) => h("li", {}, e)))),
      h("dt", {}, "Suggested next step"), h("dd", {}, f.suggestion)),
  );
}

function waterfall(spans, total) {
  const span = Math.max(total || 0, ...spans.map((s) => (s.offset_ms || 0) + (s.duration_ms || 0)), 1);
  return h(
    "div",
    { class: "table-wrap" },
    h(
      "table",
      { id: "execution" },
      h("thead", {}, h("tr", {}, h("th", {}, "Operation"), h("th", {}, "Status"), h("th", { class: "num" }, "Duration"),
        h("th", { style: "width:35%" }, "Timeline"))),
      h(
        "tbody",
        {},
        spans.map((s) => {
          const failed = s.status === "error";
          const open = s.lifecycle !== "complete";
          const left = ((s.offset_ms || 0) / span) * 100;
          const width = open ? 100 - left : ((s.duration_ms || 0) / span) * 100;
          const title = `${s.label}: ${open ? "no end recorded" : fmtMs(s.duration_ms)}, starts at +${fmtMs(s.offset_ms)}`;
          return h(
            "tr",
            { "data-span": s.span_id },
            h("td", { class: "span-name" }, h("span", { class: "span-indent", style: `width:${s.depth * 18}px` }),
              s.depth ? "└ " : "", s.label, s.orphan_reason ? h("span", { class: "badge warning" }, ` orphan: ${s.orphan_reason}`) : null),
            h("td", {}, failed
              ? h("span", { class: "badge critical" }, "✗ error", s.error_type ? ` · ${s.error_type}` : "")
              : open ? h("span", { class: "badge warning" }, "… no end") : h("span", { class: "badge good" }, "✓ ok"),
              s.attempt ? h("span", { class: "muted" }, ` attempt ${s.attempt}`) : null),
            h("td", { class: "num" }, open ? "—" : fmtMs(s.duration_ms)),
            h("td", {}, h("div", { class: "timeline", title, role: "img", "aria-label": title },
              h("div", { class: `bar${failed ? " error" : ""}${open ? " open" : ""}`,
                style: `left:${left.toFixed(2)}%;width:${Math.max(width, 0.3).toFixed(2)}%` }))),
          );
        }),
      ),
    ),
  );
}

async function viewRun(r) {
  await guarded("run", async () => {
    const data = await api(`/api/datasets/${state.dataset.id}/runs/${r.arg}`);
    const s = data.summary;
    const tin = s.tokens.input_tokens;
    const tout = s.tokens.output_tokens;
    const tokenText = !tin
      ? "no model calls"
      : tin.value === null
        ? "not reported by the provider"
        : `${fmtInt(tin.value)} in / ${tout && tout.value !== null ? fmtInt(tout.value) : "?"} out` +
          (tin.reported_spans < tin.expected_spans ? ` (partial: ${tin.reported_spans} of ${tin.expected_spans} calls reported)` : "");
    const res = s.resource || {};
    show(
      h("p", {}, link("runs", null, "← All runs")),
      h("h1", {}, s.run || "(no root span)", " ", statusBadge(s.status, data.outcome)),
      h("div", { class: "meta" },
        h("span", {}, "Trace ", h("b", { class: "mono" }, s.trace_id)),
        h("span", {}, "File ", h("b", { class: "mono" }, data.file)),
        h("span", {}, "Started ", h("b", {}, fmtTime(s.started_at))),
        h("span", {}, "Duration ", h("b", {}, fmtMs(s.duration_ms))),
        h("span", {}, "Spans ", h("b", {}, s.span_count), s.error_count ? ` (${s.error_count} failed)` : ""),
        h("span", {}, "Tokens ", h("b", { id: "run-tokens" }, tokenText)),
        h("span", {}, "Cost ", h("b", { id: "run-cost" }, data.cost ? `${data.cost.amount} ${data.cost.currency} (provider-reported)` : "not reported")),
        res.service_name ? h("span", {}, "Service ", h("b", {}, `${res.service_name} (${res.environment})`)) : null),
      h("h2", {}, `Diagnosis (${data.findings.length})`),
      data.findings.length
        ? h("div", { id: "findings" }, data.findings.map(findingCard))
        : h("p", { class: "subtle", id: "findings" }, "No issues detected by the rules."),
      h("h2", {}, "Execution"),
      waterfall(data.spans, s.duration_ms),
      h("h2", {}, "Tool calls"),
      data.tools.length
        ? h("div", { class: "table-wrap" }, h("table", {},
          h("thead", {}, h("tr", {}, h("th", {}, "Tool"), h("th", { class: "num" }, "Calls"), h("th", { class: "num" }, "Failed"),
            h("th", { class: "num" }, "Total"), h("th", { class: "num" }, "Slowest"))),
          h("tbody", {}, data.tools.map((t) => h("tr", {}, h("td", {}, t.tool), h("td", { class: "num" }, t.calls),
            h("td", { class: "num" }, t.failed), h("td", { class: "num" }, fmtMs(t.total_ms)), h("td", { class: "num" }, fmtMs(t.max_ms)))))))
        : h("p", { class: "subtle" }, "No tool calls recorded."),
      h("p", { class: "muted" }, "Prompts, responses, tool arguments and results are not shown. Error messages are secret-scrubbed."),
    );
  });
}

// ---------------------------------------------------------------- compare

function comparisonView(data) {
  const out = [];
  for (const comp of data.comparisons) {
    const b = comp.baseline;
    const c = comp.current;
    const rows = [
      ["Status", b.status, c.status],
      ["Duration", fmtMs(b.duration_ms), fmtMs(c.duration_ms)],
      ["Spans", b.span_count, c.span_count],
      ["Failed spans", b.error_count, c.error_count],
      ["Retry attempts", b.retries, c.retries],
      ["Models", b.models.join(", ") || "—", c.models.join(", ") || "—"],
      ["Input tokens", b.tokens.input_tokens.value === null ? "unknown" : fmtInt(b.tokens.input_tokens.value) + (b.tokens.input_tokens.complete ? "" : " (partial)"),
        c.tokens.input_tokens.value === null ? "unknown" : fmtInt(c.tokens.input_tokens.value) + (c.tokens.input_tokens.complete ? "" : " (partial)")],
      ["Cost (provider)", b.cost ? `${b.cost.amount} ${b.cost.currency}` : "not reported", c.cost ? `${c.cost.amount} ${c.cost.currency}` : "not reported"],
    ];
    out.push(
      h("div", { class: "table-wrap" }, h("table", { id: "compare-summary" },
        h("thead", {}, h("tr", {}, h("th", {}, ""), h("th", {}, `Baseline · ${b.run} · ${short(b.trace_id)}`), h("th", {}, `Current · ${c.run} · ${short(c.trace_id)}`))),
        h("tbody", {}, rows.map(([k, x, y]) => h("tr", {}, h("th", {}, k), h("td", {}, x), h("td", {}, y)))))),
      h("h2", {}, `Findings (${comp.findings.length})`),
      comp.findings.length ? null : h("p", { class: "subtle" }, "No regressions or changes detected."),
      comp.findings.map((f) =>
        h("article", { class: `finding ${f.severity}` },
          h("div", {}, severityBadge(f.severity), " ", h("span", { class: "rule" }, f.rule)),
          h("h3", {}, f.title),
          h("dl", {},
            h("dt", {}, "Measured"), h("dd", {}, `baseline ${f.baseline} → current ${f.current}`, h("div", { class: "muted" }, f.difference)),
            h("dt", {}, "Evidence"), h("dd", {}, h("ul", {}, f.evidence.map((e) => h("li", {}, e)))),
            h("dt", {}, "Why it matters"), h("dd", {}, f.why_it_matters),
            f.possible_causes.length ? [h("dt", {}, "Possible explanations"), h("dd", {}, "Not verified: ", f.possible_causes.join("; "))] : null,
            h("dt", {}, "Next step"), h("dd", {}, f.next_step)))),
      comp.notes.length ? h("div", { class: "notice" }, h("strong", {}, "Limits of this comparison"), h("ul", {}, comp.notes.map((n) => h("li", {}, n)))) : null,
    );
  }
  return h("div", { id: "compare-result" }, h("p", { class: "notice" }, data.disclaimer), out);
}

function fmtMetric(m, value, signed) {
  if (value === null || value === undefined) return "—";
  const sign = signed && value > 0 ? "+" : "";
  if (m.kind === "rate") return signed ? `${sign}${(value * 100).toFixed(0)} pp` : `${(value * 100).toFixed(0)}%`;
  if (m.unit === "ms") return `${sign}${value.toFixed(1)} ms`;
  if (m.unit === "tokens") return `${sign}${Math.round(value).toLocaleString("en-US")} tokens`;
  if (m.unit && m.unit.length === 3 && m.unit === m.unit.toUpperCase()) return `${sign}${Number(value.toFixed(6))} ${m.unit}`;
  return `${sign}${value.toFixed(1)} ${m.unit}`.trim();
}

function groupView(data) {
  const center = (m, side) => (side.n === 0 ? "—" : m.kind === "rate" ? fmtMetric(m, side.proportion) : fmtMetric(m, side.median));
  const table = (family, title) => {
    const rows = data.metrics.filter((m) => m.family === family);
    if (!rows.length) return null;
    return [
      h("h2", {}, title),
      h("div", { class: "table-wrap" }, h("table", { class: "metrics" },
        h("thead", {}, h("tr", {}, h("th", {}, "Metric"), h("th", { class: "num" }, "n base/cur"), h("th", { class: "num" }, "Baseline"),
          h("th", { class: "num" }, "Current"), h("th", { class: "num" }, "Change"), h("th", { class: "num" }, "p (Holm)"), h("th", {}, "Verdict"))),
        h("tbody", {}, rows.map((m) => h("tr", { "data-metric": m.key },
          h("td", {}, m.metric), h("td", { class: "num" }, `${m.baseline.n}/${m.current.n}`),
          h("td", { class: "num" }, center(m, m.baseline)), h("td", { class: "num" }, center(m, m.current)),
          h("td", { class: "num" }, fmtMetric(m, m.absolute_change, true),
            m.relative_change_pct !== null && m.kind !== "rate" ? ` (${m.relative_change_pct >= 0 ? "+" : ""}${m.relative_change_pct.toFixed(0)}%)` : ""),
          h("td", { class: "num" }, m.p_adjusted === null ? "—" : m.p_adjusted < 0.0001 ? "<0.0001" : m.p_adjusted.toFixed(4)),
          h("td", {}, verdictBadge(m.verdict))))))),
    ];
  };
  const notable = data.metrics.filter((m) => m.verdict !== "no_change" &&
    (m.verdict !== "insufficient_data" || Object.keys(m.baseline.excluded).length || Object.keys(m.current.excluded).length ||
      (m.absolute_change && Math.abs(m.relative_change_pct || 0) >= data.config.min_change_pct)));
  const card = (m) => {
    const tone = VERDICT[m.verdict] ? VERDICT[m.verdict][3] : "info";
    const shift = m.shift_estimate;
    const ci = shift && shift.ci95[0] !== null ? `, 95% CI ${fmtMetric(m, shift.ci95[0], true)} to ${fmtMetric(m, shift.ci95[1], true)}`
      : shift ? ", no confidence interval (too few runs)" : "";
    return h("article", { class: `finding ${tone}` },
      h("div", {}, verdictBadge(m.verdict), " ", h("span", { class: "rule" }, `${m.family} metric`)),
      h("h3", {}, m.metric),
      h("dl", {},
        h("dt", {}, "Baseline"), h("dd", {}, m.baseline.description),
        h("dt", {}, "Current"), h("dd", {}, m.current.description),
        h("dt", {}, "Change"), h("dd", {}, fmtMetric(m, m.absolute_change, true),
          shift ? `; shift estimate ${fmtMetric(m, shift.estimate, true)}${ci}` : ""),
        m.test ? [h("dt", {}, "Test"), h("dd", {}, `${m.test === "fisher_exact" ? "Fisher's exact test" : `Mann-Whitney U${m.exact ? " (exact)" : ""}`}: p=${m.p_value.toPrecision(3)}, Holm-adjusted p=${m.p_adjusted.toPrecision(3)} over ${m.family_size} tests`)] : null,
        m.evidence.length ? [h("dt", {}, "Evidence"), h("dd", {}, h("ul", {}, m.evidence.map((e) => h("li", {}, e))))] : null,
        m.notes.length ? [h("dt", {}, "Notes"), h("dd", {}, m.notes.join("; "))] : null,
        ["likely_regression", "weak_signal", "insufficient_data"].includes(m.verdict)
          ? [h("dt", {}, "Why it matters"), h("dd", {}, m.why_it_matters), h("dt", {}, "Next step"),
            h("dd", {}, m.verdict === "insufficient_data" ? `Record more runs (at least ${data.config.min_runs} usable per group) or close the data gap shown above.` : m.next_step)]
          : null));
  };
  return h("div", { id: "groups-result" },
    h("p", { class: "notice" }, data.disclaimer),
    h("p", { class: "muted" }, `Method: ${data.method}.`),
    h("p", {}, `Baseline: ${data.baseline.runs.length} runs (${data.baseline.finished_runs} finished) · Current: ${data.current.runs.length} runs (${data.current.finished_runs} finished)`),
    data.warnings.length ? h("div", { class: "notice warn" }, h("strong", {}, "Warnings"), h("ul", {}, data.warnings.map((w) => h("li", {}, w)))) : null,
    h("h2", {}, `Findings (${notable.length})`),
    notable.length ? notable.map(card) : h("p", { class: "subtle" }, "No meaningful change detected in any measured metric."),
    table("run", "Run-level metrics"),
    table("operation", "Per-operation metrics (exploratory)"),
    data.outliers.length ? [h("h2", {}, "Data quality: outliers"), h("ul", {}, data.outliers.map((o) => h("li", {}, o)))] : null,
    data.changes.length ? [h("h2", {}, "Other changes (not tested)"), h("ul", {}, data.changes.map((c) => h("li", {}, c)))] : null,
    h("div", { class: "notice" }, h("strong", {}, "Limits"), h("ul", {}, data.notes.map((n) => h("li", {}, n)))));
}

async function viewCompare(r) {
  await guarded("comparison options", async () => {
    const [filesData, runsData] = await Promise.all([
      api(`/api/datasets/${state.dataset.id}/files`),
      api(`/api/datasets/${state.dataset.id}/runs`),
    ]);
    const files = filesData.files;
    const mode = r.params.get("mode") || (state.dataset.presets[0] ? state.dataset.presets[0].mode : "runs");
    const result = h("div", { id: "result" });
    if (!files.length) {
      show(h("h1", {}, "Compare"), empty("Nothing to compare yet", "This dataset has no trace files."));
      return;
    }
    const runsOf = (file) => runsData.runs.filter((x) => x.file === file);
    const fileSelect = (id, value) => h("select", { id }, files.map((f) => h("option", { value: f.name, selected: f.name === value }, `${f.name} (${f.runs} run${f.runs === 1 ? "" : "s"})`)));
    const runSelect = (id, file, value) => h("select", { id }, h("option", { value: "" }, "the only run in the file"),
      runsOf(file).map((x) => h("option", { value: x.trace_id, selected: x.trace_id === value }, `${x.run} · ${x.status} · ${short(x.trace_id)}`)));
    const checks = (id, chosen) => h("div", { class: "checks", id },
      files.map((f) => h("label", {}, h("input", { type: "checkbox", value: f.name, checked: chosen.includes(f.name) }), `${f.name} (${f.runs})`)));

    const run = async (params) => {
      result.replaceChildren(loading("comparison"));
      try {
        if (params.mode === "groups") {
          const data = await api(`/api/datasets/${state.dataset.id}/compare-groups`, { baseline: params.baseline, current: params.current });
          result.replaceChildren(groupView(data));
        } else {
          const data = await api(`/api/datasets/${state.dataset.id}/compare`, params);
          result.replaceChildren(comparisonView(data));
        }
      } catch (err) {
        result.replaceChildren(err.status === 401 ? tokenForm() : errorState(err));
      }
    };

    let form;
    if (mode === "groups") {
      const base = r.params.getAll("baseline");
      const cur = r.params.getAll("current");
      const bChecks = checks("group-baseline", base.length ? base : [files[0].name]);
      const cChecks = checks("group-current", cur.length ? cur : [files[Math.min(1, files.length - 1)].name]);
      const submit = () => {
        const picked = (el) => [...el.querySelectorAll("input:checked")].map((x) => x.value);
        const params = new URLSearchParams({ mode: "groups" });
        picked(bChecks).forEach((v) => params.append("baseline", v));
        picked(cChecks).forEach((v) => params.append("current", v));
        go("compare", null, params);
      };
      form = h("div", { class: "compare-forms" },
        h("div", { class: "panel side" }, h("h3", {}, "Baseline group (all runs in the checked files)"), bChecks),
        h("div", { class: "panel side" }, h("h3", {}, "Current group"), cChecks),
        h("div", {}, h("button", { class: "primary", id: "compare-go", onclick: submit }, "Compare groups")));
      if (base.length && cur.length) run({ mode: "groups", baseline: base, current: cur });
    } else {
      const bFile = r.params.get("baseline") || files[0].name;
      const cFile = r.params.get("current") || files[Math.min(1, files.length - 1)].name;
      const bSel = fileSelect("baseline-file", bFile);
      const cSel = fileSelect("current-file", cFile);
      const bRun = runSelect("baseline-run", bFile, r.params.get("baseline_trace"));
      const cRun = runSelect("current-run", cFile, r.params.get("current_trace"));
      const refresh = (fileEl, runEl, id) => fileEl.addEventListener("change", () => runEl.replaceWith(runSelect(id, fileEl.value, "")));
      refresh(bSel, bRun, "baseline-run");
      refresh(cSel, cRun, "current-run");
      const submit = () => {
        const params = new URLSearchParams({ mode: "runs", baseline: bSel.value, current: cSel.value });
        const bt = document.getElementById("baseline-run").value;
        const ct = document.getElementById("current-run").value;
        if (bt) params.set("baseline_trace", bt);
        if (ct) params.set("current_trace", ct);
        go("compare", null, params);
      };
      form = h("div", { class: "compare-forms" },
        h("div", { class: "panel side" }, h("h3", {}, "Baseline (known good)"), h("label", {}, "File", bSel), h("label", {}, "Run", bRun)),
        h("div", { class: "panel side" }, h("h3", {}, "Current"), h("label", {}, "File", cSel), h("label", {}, "Run", cRun)),
        h("div", {}, h("button", { class: "primary", id: "compare-go", onclick: submit }, "Compare runs")));
      if (r.params.get("baseline") && r.params.get("current")) {
        run({ mode: "runs", baseline: bFile, current: cFile,
          baseline_trace: r.params.get("baseline_trace"), current_trace: r.params.get("current_trace") });
      }
    }
    const switcher = h("div", { class: "mode-switch", role: "group", "aria-label": "Comparison mode" },
      h("button", { "aria-pressed": String(mode === "runs"), onclick: () => go("compare", null, { mode: "runs" }) }, "Two runs"),
      h("button", { "aria-pressed": String(mode === "groups"), onclick: () => go("compare", null, { mode: "groups" }) }, "Groups of runs"));
    const presets = state.dataset.presets.length
      ? h("div", {}, h("div", { class: "subtle" }, "Sample comparisons (what each recording simulates):"),
        h("div", { class: "presets" }, state.dataset.presets.map((p) => h("button", {
          title: `Recording: ${p.recording}`, "data-preset": p.id,
          onclick: () => {
            const params = new URLSearchParams({ mode: p.mode });
            p.baseline.forEach((v) => params.append("baseline", v));
            p.current.forEach((v) => params.append("current", v));
            go("compare", null, params);
          },
        }, p.title))))
      : null;
    show(
      h("h1", {}, "Compare"),
      h("p", { class: "subtle" }, mode === "groups"
        ? "Compare several recordings per side with small-sample statistics (same as aobs-server compare-groups)."
        : "Compare one baseline run with one current run (same as aobs-server compare)."),
      switcher, presets, form, result,
    );
  });
}

// ---------------------------------------------------------------- start

async function start() {
  const match = window.location.hash.match(/[#&]token=([^&]+)/);
  if (match) {
    sessionStorage.setItem(TOKEN_KEY, decodeURIComponent(match[1]));
    history.replaceState(null, "", window.location.pathname);
  }
  show(loading("datasets"));
  try {
    state.datasets = (await api("/api/datasets")).datasets;
  } catch (err) {
    show(errorState(err, start));
    return;
  }
  const select = document.getElementById("dataset");
  select.replaceChildren(...state.datasets.map((d) => h("option", { value: d.id }, d.title)));
  select.addEventListener("change", () => {
    state.dataset = state.datasets.find((d) => d.id === select.value);
    go("overview");
  });
  window.addEventListener("hashchange", route);
  await route();
}

document.addEventListener("DOMContentLoaded", start);
