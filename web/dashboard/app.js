/* Callisto ops dashboard — vanilla JS, no build step.
 *
 * Three refresh cadences:
 *   - STATE_MS  (15s): live state, hypotheses, orders, portfolio, ingestion, alerts, tasks
 *   - METRICS_MS (30s): /metrics/json counters + risk report + scraper grid + eligibility
 *   - HEALTH_MS (60s): DB health, migrations, health/deep (critical ribbon)
 *
 * Every fetch is wrapped — a single failing endpoint paints its panel yellow
 * instead of blanking the whole UI.
 */

const STATE_MS = 15000;
const METRICS_MS = 30000;
const HEALTH_MS = 60000;

const API = {
  status:     "api/status",
  hyps:       "api/hypotheses/live",
  orders:     "api/orders?limit=20",
  portfolio:  "api/portfolio",
  ingestion:  "api/ingestion",
  alerts:     "api/alerts?limit=20",
  metrics:    "api/metrics",
  risk:       "api/risk-report",
  eligibility:"api/eligibility",
  scrapers:   "api/scrapers/health",
  dbhealth:   "api/db/health",
  migrations: "api/db/migrations",
  deep:       "api/health/deep",
  tasks:      "api/tasks?limit=25",
};

function apiUrl(path) {
  const base = window.location.pathname.replace(/\/$/, "");
  return `${base}/${path}`;
}

function dashboardToken() {
  const meta = document.querySelector('meta[name="callisto-dashboard-token"]');
  if (meta && meta.content) return meta.content;
  try {
    const stored = window.localStorage.getItem("callisto_dashboard_token");
    if (stored) return stored;
  } catch (_) {}
  const params = new URLSearchParams(window.location.search);
  return params.get("token") || "";
}

async function jsonFetch(path) {
  try {
    const headers = { "Accept": "application/json" };
    const tok = dashboardToken();
    if (tok) headers["X-Dashboard-Token"] = tok;
    const resp = await fetch(apiUrl(path), { cache: "no-store", headers });
    if (!resp.ok) return { __error: `HTTP ${resp.status}` };
    return await resp.json();
  } catch (e) {
    return { __error: String(e) };
  }
}

// ---------------------------------------------------------------------------
// Formatters
// ---------------------------------------------------------------------------

function fmtPct(x, digits = 1) {
  if (x == null || isNaN(x)) return "–";
  return `${Number(x).toFixed(digits)}%`;
}
function fmtNum(x, digits = 2) {
  if (x == null || isNaN(x)) return "–";
  return Number(x).toFixed(digits);
}
function fmtInt(x) {
  if (x == null || isNaN(x)) return "–";
  return Number(x).toLocaleString("en-US", { maximumFractionDigits: 0 });
}
function fmtAge(seconds) {
  if (seconds == null) return "–";
  seconds = Number(seconds);
  if (seconds < 60) return `${Math.round(seconds)}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${(seconds / 3600).toFixed(1)}h ago`;
  return `${(seconds / 86400).toFixed(1)}d ago`;
}
function fmtMoney(x) {
  if (x == null || isNaN(x)) return "–";
  return "$" + Number(x).toFixed(2);
}
function fmtMb(x) {
  if (x == null || isNaN(x)) return "–";
  return `${Number(x).toFixed(2)} MB`;
}
function ageSince(iso) {
  if (!iso) return null;
  const t = new Date(iso).getTime();
  if (isNaN(t)) return null;
  return (Date.now() - t) / 1000;
}

function escapeHtml(str) {
  if (str == null) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function pill(text, color) {
  const c = String(color || "muted").toLowerCase();
  const cls = ["green", "yellow", "red", "muted"].includes(c) ? c : "muted";
  return `<span class="pill pill-${cls}">${escapeHtml(text)}</span>`;
}

function setOnline(online) {
  const pillEl = document.getElementById("online-pill");
  const banner = document.getElementById("offline-banner");
  if (online) {
    pillEl.textContent = "online";
    pillEl.className = "pill pill-green";
    banner.classList.add("hidden");
  } else {
    pillEl.textContent = "offline";
    pillEl.className = "pill pill-red";
    banner.classList.remove("hidden");
  }
}

function setLastRefresh() {
  const el = document.getElementById("last-refresh");
  const ts = new Date();
  el.textContent = `refreshed ${ts.toLocaleTimeString()}`;
}

function panelError(id, msg) {
  const body = document.getElementById(id);
  if (body) body.innerHTML = `<div class="error">${escapeHtml(msg)}</div>`;
}

// ---------------------------------------------------------------------------
// Renderers — original 6 panels
// ---------------------------------------------------------------------------

function renderState(data) {
  const body = document.getElementById("state-body");
  if (data.__error || !data.online) {
    body.innerHTML = `<div class="error">Main API unreachable: ${escapeHtml(data.__error || "offline")}</div>`;
    setOnline(false);
    return;
  }
  setOnline(true);
  const full = data.full_status || {};
  const auto = full.autonomous_loop || {};
  const rl = full.research_loop || {};
  const lm = full.line_monitor || {};
  const exec = data.executor || {};
  const claude = data.claude || {};

  let loopState = "unknown";
  if (rl.local_only) loopState = "local_only";
  else if (rl.paused) loopState = "paused";
  else if (rl.running || auto.running) loopState = "running";

  const snapshotAge = fmtAge(ageSince(lm.latest_snapshot_at));

  body.innerHTML = `
    <dl class="kv">
      <dt>Loop state</dt>
      <dd>${pill(loopState, loopState === "running" ? "green" : loopState === "paused" ? "yellow" : "red")}</dd>

      <dt>Research cycles</dt>
      <dd>${rl.cycles_completed ?? "–"} / hypotheses ${rl.hypotheses_generated ?? 0} / backtests ${rl.backtests_run ?? 0}</dd>

      <dt>Claude calls</dt>
      <dd>${claude.calls_this_window ?? "–"} / ${claude.max_calls_per_hour ?? "–"}
          <span class="muted">(${claude.available ? "avail" : "cooldown"})</span></dd>

      <dt>Line monitor</dt>
      <dd>${(lm.monitored_sports || []).length} sports · last snapshot ${snapshotAge}</dd>

      <dt>Cached snapshots</dt>
      <dd>${(lm.cached_snapshots || []).join(", ") || "–"}</dd>

      <dt>Executor</dt>
      <dd>${pill(exec.enabled ? "enabled" : "disabled", exec.enabled ? "green" : "muted")}
          ${exec.logged_in ? "logged in" : "logged out"} ·
          daily loss cap ${fmtMoney(exec.daily_loss_limit)}</dd>

      <dt>Circuit breakers</dt>
      <dd>${renderCircuits(full.system_health || {}, data.health || {})}</dd>

      <dt>Hypotheses</dt>
      <dd>${(full.hypotheses || {}).live ?? 0} live · ${(full.hypotheses || {}).paper_trading ?? 0} paper · ${(full.hypotheses || {}).total ?? 0} total</dd>
    </dl>
  `;
}

function renderCircuits(sysHealth, health) {
  const subs = (sysHealth && sysHealth.subsystems) || health.subsystems || {};
  const trips = (sysHealth && sysHealth.trip_history) || [];
  const keys = Object.keys(subs);
  if (!keys.length) return '<span class="muted">no data</span>';
  const open = keys.filter(k => subs[k] && subs[k].is_open);
  const recentTripTxt = trips.length ? ` · ${trips.length} recent trip${trips.length === 1 ? "" : "s"}` : "";
  if (open.length === 0) {
    return `${pill("all closed", "green")}${recentTripTxt}`;
  }
  return `${pill(`${open.length} open`, "red")}: ${open.join(", ")}${recentTripTxt}`;
}

function renderHyps(data) {
  const body = document.getElementById("hyps-body");
  const countEl = document.getElementById("hyp-count");
  if (data.__error) {
    body.innerHTML = `<div class="error">${escapeHtml(data.__error)}</div>`;
    countEl.textContent = "";
    return;
  }
  const hyps = data.hypotheses || [];
  countEl.textContent = hyps.length ? `${hyps.length} live` : "";
  if (!hyps.length) {
    body.innerHTML = '<div class="empty">No LIVE hypotheses.</div>';
    return;
  }
  body.innerHTML =
    `<div class="hyp-grid">` +
    hyps.map(h => {
      const color = h.health_color || "yellow";
      return `
        <div class="hyp-card health-${color}">
          <div class="name">${escapeHtml(h.name || h.hypothesis_id || "–")}</div>
          <div class="meta">${escapeHtml(h.sport || "")} · ${escapeHtml(h.market_type || "")} · ${pill(h.status || "–", color)}</div>
          <div class="stats">
            <span class="k">days live</span><span class="v">${fmtNum(h.days_live, 1)}</span>
            <span class="k">signals</span><span class="v">${h.recent_signals ?? "–"}</span>
            <span class="k">hit</span><span class="v">${h.rolling_hit_rate != null ? fmtPct(h.rolling_hit_rate * 100) : "–"}</span>
            <span class="k">ROI</span><span class="v">${h.rolling_roi != null ? fmtPct(h.rolling_roi * 100) : "–"}</span>
            <span class="k">CLV</span><span class="v">${h.rolling_clv != null ? fmtPct(h.rolling_clv * 100) : "–"}</span>
            <span class="k">id</span><span class="v muted">${escapeHtml((h.hypothesis_id || "").slice(0, 11))}</span>
          </div>
        </div>
      `;
    }).join("") +
    `</div>`;
}

function renderOrders(data) {
  const body = document.getElementById("orders-body");
  const countEl = document.getElementById("orders-count");
  if (data.__error) {
    body.innerHTML = `<div class="error">${escapeHtml(data.__error)}</div>`;
    countEl.textContent = "";
    return;
  }
  const orders = data.orders || [];
  const counts = data.counts_by_state || {};
  const countsTxt = Object.entries(counts)
    .map(([k, v]) => `${k}: ${v}`).join(" · ") || "none";
  countEl.textContent = `${orders.length} rows · ${countsTxt}${data.source === "db" ? " (db fallback)" : ""}`;

  if (!orders.length) {
    body.innerHTML = '<div class="empty">No recent orders.</div>';
    return;
  }
  const rows = orders.map(o => {
    const state = String(o.state || o.status || "").toLowerCase();
    const pending = state.includes("pending");
    return `
      <tr class="${pending ? "pending" : ""}">
        <td>${escapeHtml(o.id ?? "–")}</td>
        <td>${pill(state || "–", pending ? "yellow" : state === "filled" ? "green" : state === "rejected" ? "red" : "muted")}</td>
        <td>${escapeHtml(o.sport || "–")}</td>
        <td>${escapeHtml(o.market || o.market_type || "–")}</td>
        <td>${fmtMoney(o.stake)}</td>
        <td class="muted">${escapeHtml(o.created_at || o.submitted_at || "–")}</td>
      </tr>`;
  }).join("");
  body.innerHTML = `
    <table class="tbl">
      <thead><tr><th>#</th><th>state</th><th>sport</th><th>market</th><th>stake</th><th>created</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

function renderPortfolio(data) {
  const body = document.getElementById("portfolio-body");
  if (data.__error || !data.online) {
    body.innerHTML = `<div class="error">${escapeHtml(data.__error || "offline")}</div>`;
    return;
  }
  const dd = data.drawdown_pct;
  const ddColor = dd == null ? "muted" : dd < 2 ? "green" : dd < 8 ? "yellow" : "red";
  const exposureRows = Object.entries(data.exposure_by_sport || {})
    .sort((a, b) => b[1] - a[1])
    .map(([s, v]) => `<tr><td>${escapeHtml(s)}</td><td>${fmtMoney(v)}</td></tr>`)
    .join("") || `<tr><td class="empty" colspan="2">no open exposure</td></tr>`;

  body.innerHTML = `
    <dl class="kv">
      <dt>Bankroll</dt><dd>${fmtMoney(data.current_balance)}</dd>
      <dt>Rolling peak</dt><dd>${fmtMoney(data.rolling_peak)}</dd>
      <dt>Drawdown</dt><dd>${pill(fmtPct(dd, 2), ddColor)}</dd>
      <dt>Open exposure</dt><dd>${fmtMoney(data.total_open_exposure)} · ${data.unsettled_count || 0} bets</dd>
    </dl>
    <table class="tbl" style="margin-top:6px">
      <thead><tr><th>sport</th><th>exposure</th></tr></thead>
      <tbody>${exposureRows}</tbody>
    </table>`;
}

function renderIngestion(data) {
  const body = document.getElementById("ingestion-body");
  if (data.__error) {
    body.innerHTML = `<div class="error">${escapeHtml(data.__error)}</div>`;
    return;
  }

  let rows = [];
  if (data.sla_report && typeof data.sla_report === "object") {
    const sla = data.sla_report;
    if (Array.isArray(sla.sources)) rows = sla.sources;
    else {
      rows = Object.entries(sla).map(([source, v]) => ({ source, ...(v || {}) }));
    }
  } else if (Array.isArray(data.sources)) {
    rows = data.sources;
  }

  if (!rows.length) {
    body.innerHTML = '<div class="empty">No ingestion telemetry available.</div>';
    return;
  }

  const tbody = rows.map(r => {
    const status = r.sla_status || classifySla(r.age_seconds, r.sla_seconds);
    return `<tr>
      <td>${escapeHtml(r.source || "–")}</td>
      <td>${pill(status, status)}</td>
      <td>${escapeHtml(r.status || "–")}</td>
      <td>${fmtAge(r.age_seconds)}</td>
      <td>${r.sla_seconds != null ? r.sla_seconds + "s" : "–"}</td>
      <td>${r.rows_written ?? "–"}</td>
    </tr>`;
  }).join("");

  body.innerHTML = `
    <table class="tbl">
      <thead><tr><th>source</th><th>SLA</th><th>last status</th><th>last success</th><th>SLA win</th><th>rows</th></tr></thead>
      <tbody>${tbody}</tbody>
    </table>
    <div class="muted" style="margin-top:6px">source: ${escapeHtml(data.source || "–")}</div>`;
}

function classifySla(age, sla) {
  if (age == null || sla == null) return "muted";
  if (age <= sla) return "green";
  if (age <= sla * 3) return "yellow";
  return "red";
}

function renderAlerts(data) {
  const body = document.getElementById("alerts-body");
  const alerts = data.alerts || [];
  if (data.__error) {
    body.innerHTML = `<div class="error">${escapeHtml(data.__error)}</div>`;
    return;
  }
  if (!alerts.length) {
    body.innerHTML = '<div class="empty">No recent alerts.</div>';
    return;
  }
  const rows = alerts.map(a => {
    const ts = a.timestamp || a.created_at || a.tripped_at || "";
    const msg = a.message || a.reason || a.subsystem || a.description || JSON.stringify(a).slice(0, 120);
    return `<tr><td class="muted">${escapeHtml(ts)}</td><td>${escapeHtml(a._source_table || "–")}</td><td>${escapeHtml(msg)}</td></tr>`;
  }).join("");
  body.innerHTML = `
    <table class="tbl">
      <thead><tr><th>ts</th><th>source</th><th>detail</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

// ---------------------------------------------------------------------------
// Renderers — new panels
// ---------------------------------------------------------------------------

function aggregateMetric(metricsArr, name) {
  if (!Array.isArray(metricsArr)) return null;
  return metricsArr.find(m => m && m.name === name) || null;
}

function sumSamples(metric) {
  if (!metric || !Array.isArray(metric.samples)) return 0;
  return metric.samples.reduce((a, s) => a + (Number(s.value) || 0), 0);
}

function topLabelBreakdown(metric, labelKey, topN = 6) {
  if (!metric || !Array.isArray(metric.samples)) return [];
  const agg = new Map();
  for (const s of metric.samples) {
    const k = (s.labels && s.labels[labelKey]) || "";
    agg.set(k, (agg.get(k) || 0) + (Number(s.value) || 0));
  }
  return [...agg.entries()]
    .sort((a, b) => b[1] - a[1])
    .slice(0, topN)
    .map(([k, v]) => ({ label: k || "–", value: v }));
}

function pairLabelBreakdown(metric, labelA, labelB, topN = 8) {
  if (!metric || !Array.isArray(metric.samples)) return [];
  const agg = new Map();
  for (const s of metric.samples) {
    const a = (s.labels && s.labels[labelA]) || "–";
    const b = (s.labels && s.labels[labelB]) || "–";
    const k = `${a} / ${b}`;
    agg.set(k, (agg.get(k) || 0) + (Number(s.value) || 0));
  }
  return [...agg.entries()]
    .sort((a, b) => b[1] - a[1])
    .slice(0, topN)
    .map(([k, v]) => ({ label: k, value: v }));
}

function renderMetrics(data) {
  const body = document.getElementById("metrics-body");
  const countEl = document.getElementById("metrics-count");
  if (data.__error || !data.online) {
    body.innerHTML = `<div class="error">${escapeHtml(data.__error || "offline")}</div>`;
    countEl.textContent = "";
    return;
  }
  const metrics = Array.isArray(data.metrics) ? data.metrics : [];
  countEl.textContent = metrics.length ? `${metrics.length} series` : "";

  const tasksSub   = aggregateMetric(metrics, "callisto_tasks_submitted_total");
  const tasksDone  = aggregateMetric(metrics, "callisto_tasks_completed_total");
  const hypsCr     = aggregateMetric(metrics, "callisto_hypotheses_created_total");
  const edges      = aggregateMetric(metrics, "callisto_edges_detected_total");
  const bets       = aggregateMetric(metrics, "callisto_bets_placed_total");
  const bankrollG  = aggregateMetric(metrics, "callisto_bets_bankroll_gauge");
  const claudeC    = aggregateMetric(metrics, "callisto_claude_calls_total");

  const tasksTotal  = sumSamples(tasksSub);
  const tasksEnded  = sumSamples(tasksDone);
  const hypsTotal   = sumSamples(hypsCr);
  const edgesTotal  = sumSamples(edges);
  const betsTotal   = sumSamples(bets);
  const bankroll    = bankrollG && bankrollG.samples && bankrollG.samples[0]
                        ? Number(bankrollG.samples[0].value) : null;
  const claudeTotal = sumSamples(claudeC);

  const hypsBySport = topLabelBreakdown(hypsCr, "sport");
  const edgesBySM   = pairLabelBreakdown(edges, "sport", "market");
  const betsBySM    = pairLabelBreakdown(bets, "sport", "market");

  const renderBreakdown = (rows, emptyMsg) => {
    if (!rows.length) return `<div class="empty">${emptyMsg}</div>`;
    return `<table class="tbl tbl-compact">
      <tbody>${rows.map(r => `
        <tr>
          <td>${escapeHtml(r.label)}</td>
          <td class="num">${fmtInt(r.value)}</td>
        </tr>`).join("")}
      </tbody>
    </table>`;
  };

  const uptimeH = data.uptime_seconds != null
    ? `${(data.uptime_seconds / 3600).toFixed(1)}h` : "–";

  body.innerHTML = `
    <div class="metric-tiles">
      <div class="tile">
        <div class="tile-label">tasks submitted</div>
        <div class="tile-value">${fmtInt(tasksTotal)}</div>
        <div class="tile-sub muted">${fmtInt(tasksEnded)} completed</div>
      </div>
      <div class="tile">
        <div class="tile-label">hypotheses created</div>
        <div class="tile-value">${fmtInt(hypsTotal)}</div>
        <div class="tile-sub muted">${hypsBySport.length} sports</div>
      </div>
      <div class="tile">
        <div class="tile-label">edges detected</div>
        <div class="tile-value">${fmtInt(edgesTotal)}</div>
        <div class="tile-sub muted">${edgesBySM.length} sport/market</div>
      </div>
      <div class="tile">
        <div class="tile-label">bets placed</div>
        <div class="tile-value">${fmtInt(betsTotal)}</div>
        <div class="tile-sub muted">${betsBySM.length} series</div>
      </div>
      <div class="tile">
        <div class="tile-label">bankroll</div>
        <div class="tile-value">${bankroll != null ? fmtMoney(bankroll) : "–"}</div>
        <div class="tile-sub muted">claude ${fmtInt(claudeTotal)}</div>
      </div>
      <div class="tile">
        <div class="tile-label">uptime</div>
        <div class="tile-value">${uptimeH}</div>
        <div class="tile-sub muted">${fmtInt(metrics.length)} metrics</div>
      </div>
    </div>

    <div class="metric-cols">
      <div>
        <div class="sub-h">hypotheses by sport</div>
        ${renderBreakdown(hypsBySport, "no samples")}
      </div>
      <div>
        <div class="sub-h">edges by sport/market</div>
        ${renderBreakdown(edgesBySM, "no samples")}
      </div>
      <div>
        <div class="sub-h">bets by sport/market</div>
        ${renderBreakdown(betsBySM, "no samples")}
      </div>
    </div>
  `;
}

function renderRisk(data) {
  const body = document.getElementById("risk-body");
  if (data.__error || !data.online) {
    body.innerHTML = `<div class="error">${escapeHtml(data.__error || "offline")}</div>`;
    return;
  }
  const meter = (label, amount, cap, util) => {
    const u = util != null ? Number(util) : (cap > 0 ? amount / cap : 0);
    const pct = Math.min(u * 100, 100);
    const color = u >= 1 ? "red" : u >= 0.75 ? "yellow" : "green";
    return `
      <div class="meter">
        <div class="meter-head">
          <span>${escapeHtml(label)}</span>
          <span class="muted">${fmtMoney(amount)} / ${fmtMoney(cap)} (${fmtPct(u * 100, 1)})</span>
        </div>
        <div class="meter-bar">
          <div class="meter-fill meter-${color}" style="width:${pct.toFixed(1)}%"></div>
        </div>
      </div>`;
  };

  const openEx = data.open_exposure || {};
  const daily  = data.daily_risk || {};
  const pnl    = data.daily_pnl || {};
  const tripped = Array.isArray(data.tripped_breakers) ? data.tripped_breakers : [];

  const bankrollRow = `
    <dl class="kv">
      <dt>Bankroll</dt><dd>${fmtMoney(data.bankroll)}</dd>
      <dt>Rolling peak</dt><dd>${fmtMoney(data.rolling_peak)} · drawdown ${fmtPct((data.drawdown_pct || 0) * 100, 2)}</dd>
      <dt>Daily P/L</dt><dd>${fmtMoney(pnl.net)} · loss cap ${fmtMoney(pnl.loss_cap)}</dd>
      <dt>Tripped</dt><dd>${tripped.length ? tripped.map(t => pill(t, "red")).join(" ") : pill("none", "green")}</dd>
    </dl>`;

  const sportRows = Object.entries(data.per_sport || {})
    .sort((a, b) => (b[1].utilization || 0) - (a[1].utilization || 0))
    .slice(0, 8)
    .map(([sport, s]) => meter(sport, s.exposure, s.cap, s.utilization))
    .join("") || `<div class="empty">no per-sport exposure</div>`;

  body.innerHTML = `
    ${bankrollRow}
    ${meter("Open exposure", openEx.amount, openEx.cap, openEx.utilization)}
    ${meter("Daily risk (stakes today)", daily.stakes_today, daily.cap, daily.utilization)}
    <div class="sub-h" style="margin-top:8px">per-sport exposure</div>
    ${sportRows}`;
}

function renderEligibility(data) {
  const body = document.getElementById("eligibility-body");
  if (data.__error || !data.online) {
    body.innerHTML = `<div class="error">${escapeHtml(data.__error || "offline")}</div>`;
    return;
  }
  const e = data.eligibility;
  if (!e) {
    body.innerHTML = '<div class="empty">No eligibility snapshot yet — research loop has not run.</div>';
    return;
  }
  const eligible = e.eligible_sports || [];
  const blockedGames = e.blocked_by_games || {};
  const blockedOdds = e.blocked_by_odds || [];
  const min = e.min_games_for_hypothesis;
  const researchSports = e.research_sports || data.research_sports || [];

  const eligibleHtml = eligible.length
    ? eligible.map(s => pill(s, "green")).join(" ")
    : pill("NONE", "red");

  const blockedGamesRows = Object.entries(blockedGames)
    .sort((a, b) => a[0].localeCompare(b[0]))
    .map(([s, gc]) => `<tr><td>${escapeHtml(s)}</td><td>${gc} &lt; ${min}</td><td>${pill("no games", "red")}</td></tr>`)
    .join("");
  const blockedOddsRows = (blockedOdds || []).map(s =>
    `<tr><td>${escapeHtml(s)}</td><td>ok</td><td>${pill("no odds", "yellow")}</td></tr>`
  ).join("");

  const blockedHtml = (blockedGamesRows || blockedOddsRows)
    ? `<table class="tbl tbl-compact">
        <thead><tr><th>sport</th><th>games</th><th>reason</th></tr></thead>
        <tbody>${blockedGamesRows}${blockedOddsRows}</tbody>
      </table>`
    : `<div class="empty">all sports eligible</div>`;

  const age = e.at ? fmtAge((Date.now() / 1000) - Number(e.at)) : "–";

  body.innerHTML = `
    <dl class="kv">
      <dt>Eligible</dt><dd>${eligibleHtml}</dd>
      <dt>Universe</dt><dd>${(researchSports || []).map(s => escapeHtml(s)).join(", ") || "–"}</dd>
      <dt>Min games</dt><dd>${min ?? "–"} · cycle ${e.cycle ?? "–"} · snapshot ${age}</dd>
    </dl>
    <div class="sub-h" style="margin-top:6px">blocked sports</div>
    ${blockedHtml}`;
}

function renderScrapers(data) {
  const body = document.getElementById("scrapers-body");
  const countEl = document.getElementById("scraper-count");
  if (data.__error || !data.online) {
    body.innerHTML = `<div class="error">${escapeHtml(data.__error || "offline")}</div>`;
    countEl.textContent = "";
    return;
  }
  const scrapers = data.scrapers || [];
  countEl.textContent = scrapers.length
    ? `${scrapers.length} · ${data.stale_count || 0} stale · ${data.never_pulled_count || 0} never`
    : "";
  if (!scrapers.length) {
    body.innerHTML = '<div class="empty">No scrapers registered yet.</div>';
    return;
  }
  const cells = scrapers.map(s => {
    let color = "green";
    if (!s.last_successful_pull) color = "muted";
    else if (!s.healthy) color = "red";
    else if (s.consecutive_errors && s.consecutive_errors > 0) color = "yellow";
    const age = s.staleness_s != null ? fmtAge(s.staleness_s) : "never";
    return `
      <div class="scraper-card scraper-${color}">
        <div class="scraper-light scraper-light-${color}"></div>
        <div class="scraper-body">
          <div class="scraper-name">${escapeHtml(s.name)}</div>
          <div class="scraper-meta muted">${age} · ok=${s.success_count ?? 0} · err=${s.error_count ?? 0}</div>
          ${s.last_error ? `<div class="scraper-err">${escapeHtml(String(s.last_error).slice(0, 80))}</div>` : ""}
        </div>
      </div>`;
  }).join("");
  body.innerHTML = `<div class="scraper-grid">${cells}</div>`;
}

// WAL/busy-hits history kept in-memory — tiny ring buffer so we can sparkline trends.
const _dbHistory = {
  wal: [],
  busy: [],
  ckpt: [],
  max: 40,
};

function pushHistory(key, value) {
  if (!Number.isFinite(value)) return;
  _dbHistory[key].push(value);
  if (_dbHistory[key].length > _dbHistory.max) _dbHistory[key].shift();
}

function sparkline(values, width = 140, height = 28) {
  if (!values || values.length < 2) {
    return `<svg width="${width}" height="${height}"><text x="2" y="${height - 6}" class="spark-empty">no history yet</text></svg>`;
  }
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const step = width / (values.length - 1);
  const points = values.map((v, i) => {
    const x = (i * step).toFixed(1);
    const y = (height - 2 - ((v - min) / span) * (height - 4)).toFixed(1);
    return `${x},${y}`;
  }).join(" ");
  return `<svg class="spark" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">
    <polyline fill="none" stroke-width="1.4" points="${points}" />
  </svg>`;
}

function renderDbHealth(data) {
  const body = document.getElementById("dbhealth-body");
  if (data.__error || !data.online) {
    body.innerHTML = `<div class="error">${escapeHtml(data.__error || "offline")}</div>`;
    return;
  }
  pushHistory("wal", Number(data.wal_size_mb) || 0);
  const busy = (data.busy_timeout_hits && typeof data.busy_timeout_hits === "object")
    ? Number(
        data.busy_timeout_hits.hits_in_window
        ?? data.busy_timeout_hits.hits_last_hour
        ?? data.busy_timeout_hits.total_tracked
        ?? data.busy_timeout_hits.total
        ?? 0
      )
    : Number(data.busy_timeout_hits) || 0;
  pushHistory("busy", busy);
  const maintenance = data.maintenance || {};
  const ckptDur = Number(maintenance.last_checkpoint_duration_s ?? maintenance.last_checkpoint_s ?? 0) || 0;
  pushHistory("ckpt", ckptDur);

  const walColor = (Number(data.wal_size_mb) || 0) < 32 ? "green"
                 : (Number(data.wal_size_mb) || 0) < 128 ? "yellow" : "red";
  const busyColor = busy === 0 ? "green" : busy < 10 ? "yellow" : "red";

  body.innerHTML = `
    <dl class="kv">
      <dt>WAL size</dt>
      <dd>${pill(fmtMb(data.wal_size_mb), walColor)} · ${fmtInt(data.wal_page_count)} pages
          <div class="spark-wrap">${sparkline(_dbHistory.wal)}</div></dd>

      <dt>DB size</dt>
      <dd>${fmtMb(data.db_size_mb)} · ${fmtInt(data.db_page_count)} pages · frag ${fmtPct((data.fragmentation_ratio || 0) * 100, 2)}</dd>

      <dt>Busy-timeout hits (1h)</dt>
      <dd>${pill(fmtInt(busy), busyColor)}
          <div class="spark-wrap">${sparkline(_dbHistory.busy)}</div></dd>

      <dt>Last checkpoint</dt>
      <dd>${fmtNum(ckptDur, 3)}s · checkpointed ${fmtInt(data.wal_checkpointed_now)}
          <div class="spark-wrap">${sparkline(_dbHistory.ckpt)}</div></dd>

      <dt>Journal mode</dt>
      <dd>${escapeHtml(data.journal_mode || "–")} · freelist ${fmtInt(data.freelist_pages)}</dd>
    </dl>`;
}

function renderMigrations(data) {
  const body = document.getElementById("migrations-body");
  if (data.__error || !data.online) {
    body.innerHTML = `<div class="error">${escapeHtml(data.__error || "offline")}</div>`;
    return;
  }
  const applied = Array.isArray(data.applied) ? data.applied : [];
  const pending = Array.isArray(data.pending) ? data.pending : [];
  const drift = Array.isArray(data.drift) ? data.drift : [];

  const version = data.schema_version ?? "–";
  const versionColor = pending.length || drift.length ? "yellow" : "green";

  const pendingHtml = pending.length
    ? `<div class="mig-pending">
         <div class="sub-h">pending (${pending.length})</div>
         <ul class="mig-list">${pending.map(p =>
           `<li>${pill("PENDING", "yellow")} v${p.version} · ${escapeHtml(p.name || p.module || "–")}</li>`
         ).join("")}</ul>
       </div>`
    : `<div class="empty">no pending migrations</div>`;

  const driftHtml = drift.length
    ? `<div class="mig-drift">
         <div class="sub-h">checksum drift (${drift.length})</div>
         <ul class="mig-list">${drift.map(d =>
           `<li>${pill("DRIFT", "red")} v${d.version} · ${escapeHtml(d.name)}</li>`
         ).join("")}</ul>
       </div>`
    : "";

  const recent = applied.slice(-5);
  const appliedHtml = recent.length
    ? `<div class="sub-h">last applied</div>
       <ul class="mig-list">${recent.map(a =>
         `<li>${pill("v" + a.version, "green")} ${escapeHtml(a.name)} <span class="muted">${escapeHtml(a.applied_at || "")}</span></li>`
       ).join("")}</ul>`
    : "";

  body.innerHTML = `
    <dl class="kv">
      <dt>Schema version</dt><dd>${pill(String(version), versionColor)}</dd>
      <dt>Applied</dt><dd>${applied.length}</dd>
    </dl>
    ${pendingHtml}
    ${driftHtml}
    ${appliedHtml}`;
}

// ---------------------------------------------------------------------------
// Tasks panel with live filter
// ---------------------------------------------------------------------------

let _tasksRaw = [];

function renderTasksTable() {
  const body = document.getElementById("tasks-body");
  const countEl = document.getElementById("tasks-count");
  const hint = document.getElementById("task-filter-hint");
  const filter = (document.getElementById("task-filter")?.value || "").trim().toLowerCase();

  const filtered = filter
    ? _tasksRaw.filter(t => {
        const s = String(t.status || "").toLowerCase();
        const q = String(t.query || t.prompt || "").toLowerCase();
        const id = String(t.id || t.task_id || "");
        return s.includes(filter) || q.includes(filter) || id.includes(filter);
      })
    : _tasksRaw;

  countEl.textContent = _tasksRaw.length
    ? `${filtered.length}/${_tasksRaw.length}`
    : "";
  hint.textContent = filter
    ? `filter: "${filter}" — ${filtered.length} match${filtered.length === 1 ? "" : "es"}`
    : "";

  if (!filtered.length) {
    body.innerHTML = filter
      ? `<div class="empty">No tasks match "${escapeHtml(filter)}".</div>`
      : `<div class="empty">No recent tasks.</div>`;
    return;
  }

  const rows = filtered.map(t => {
    const st = String(t.status || "").toLowerCase();
    const color = st === "completed" ? "green"
                : st === "failed" || st === "timeout" ? "red"
                : st === "processing" || st === "pending" ? "yellow" : "muted";
    const q = String(t.query || t.prompt || "").slice(0, 140);
    const created = t.created_at || t.submitted_at || "";
    return `<tr>
      <td class="muted">${escapeHtml(t.id ?? t.task_id ?? "–")}</td>
      <td>${pill(st || "–", color)}</td>
      <td>${escapeHtml(q)}</td>
      <td class="muted">${escapeHtml(created)}</td>
    </tr>`;
  }).join("");

  body.innerHTML = `
    <table class="tbl">
      <thead><tr><th>id</th><th>status</th><th>query</th><th>created</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

function renderTasks(data) {
  if (data.__error || data.online === false) {
    _tasksRaw = [];
    const body = document.getElementById("tasks-body");
    body.innerHTML = `<div class="error">${escapeHtml(data.__error || "offline")}</div>`;
    return;
  }
  _tasksRaw = Array.isArray(data.tasks) ? data.tasks
           : Array.isArray(data) ? data : [];
  renderTasksTable();
}

// ---------------------------------------------------------------------------
// Critical alerts ribbon — fed by /health/deep
// ---------------------------------------------------------------------------

function renderCriticalRibbon(data) {
  const ribbon = document.getElementById("critical-ribbon");
  const bodyEl = document.getElementById("critical-ribbon-body");
  if (!ribbon || !bodyEl) return;

  const criticals = [];

  if (data && !data.__error && data.online !== false) {
    // Primary shape: /health/deep returns issue_details[] with severity "CRITICAL"
    const issueSources = [];
    if (Array.isArray(data.issue_details)) issueSources.push(data.issue_details);
    if (Array.isArray(data.checks)) issueSources.push(data.checks);
    if (Array.isArray(data.integrity_checks)) issueSources.push(data.integrity_checks);
    for (const arr of issueSources) {
      for (const c of arr) {
        const sev = String(c.severity || c.level || "").toLowerCase();
        if (sev === "critical") {
          criticals.push(
            (c.check ? `[${c.check}] ` : "") +
            (c.message || c.name || "critical check failed")
          );
        }
      }
    }
    // Counters shape: data.issues = {critical: N, warning: N, ...}
    if (!criticals.length && data.issues && typeof data.issues === "object"
        && Number(data.issues.critical) > 0) {
      criticals.push(`${data.issues.critical} critical issue${data.issues.critical === 1 ? "" : "s"} reported by /health/deep`);
    }
    // Pipeline integrity block
    const pi = data.pipeline_integrity;
    if (pi && typeof pi === "object") {
      const status = String(pi.status || "").toUpperCase();
      if (status === "BROKEN" || status === "CRITICAL") {
        criticals.push(`pipeline ${status}: ${pi.summary || pi.message || "integrity failure"}`);
      }
    }
    // Subsystem breakers explicitly flagged critical
    const subsRoot = data.subsystems || {};
    const subs = subsRoot.subsystems || subsRoot;
    for (const [k, v] of Object.entries(subs || {})) {
      if (v && typeof v === "object" && v.is_open
          && (String(v.severity || "").toLowerCase() === "critical" || v.critical === true)) {
        criticals.push(`subsystem ${k} open: ${v.last_reason || v.reason || "tripped"}`);
      }
    }
  }

  if (!criticals.length) {
    ribbon.classList.add("hidden");
    bodyEl.innerHTML = "";
    return;
  }
  ribbon.classList.remove("hidden");
  bodyEl.innerHTML = criticals
    .slice(0, 6)
    .map(c => escapeHtml(String(c).slice(0, 240)))
    .join(" &middot; ");
}

// ---------------------------------------------------------------------------
// Refresh loops — three cadences
// ---------------------------------------------------------------------------

async function refreshState() {
  const [status, hyps, orders, portfolio, ingestion, alerts, tasks] = await Promise.all([
    jsonFetch(API.status),
    jsonFetch(API.hyps),
    jsonFetch(API.orders),
    jsonFetch(API.portfolio),
    jsonFetch(API.ingestion),
    jsonFetch(API.alerts),
    jsonFetch(API.tasks),
  ]);
  renderState(status);
  renderHyps(hyps);
  renderOrders(orders);
  renderPortfolio(portfolio);
  renderIngestion(ingestion);
  renderAlerts(alerts);
  renderTasks(tasks);
  setLastRefresh();
}

async function refreshMetrics() {
  const [metrics, risk, scrapers, eligibility] = await Promise.all([
    jsonFetch(API.metrics),
    jsonFetch(API.risk),
    jsonFetch(API.scrapers),
    jsonFetch(API.eligibility),
  ]);
  renderMetrics(metrics);
  renderRisk(risk);
  renderScrapers(scrapers);
  renderEligibility(eligibility);
}

async function refreshHealth() {
  const [dbh, mig, deep] = await Promise.all([
    jsonFetch(API.dbhealth),
    jsonFetch(API.migrations),
    jsonFetch(API.deep),
  ]);
  renderDbHealth(dbh);
  renderMigrations(mig);
  renderCriticalRibbon(deep);
}

function wireFilters() {
  const input = document.getElementById("task-filter");
  if (!input) return;
  input.addEventListener("input", () => renderTasksTable());
}

wireFilters();
refreshState();
refreshMetrics();
refreshHealth();
setInterval(refreshState, STATE_MS);
setInterval(refreshMetrics, METRICS_MS);
setInterval(refreshHealth, HEALTH_MS);
