/* Callisto research appliance dashboard — vanilla JS, no build step.
 *
 * Refresh model: poll 3 endpoints every REFRESH_MS. Each panel renders
 * independently so one slow/failed endpoint never blanks the whole UI.
 * Every fetch is wrapped — the error path just tags the panel with a
 * yellow "stale" banner instead of exploding.
 */

const REFRESH_MS = 15000;

// All endpoints are relative; the dashboard sub-app mounts at its own
// root, so "/api/..." resolves correctly whether we're standalone on
// 8421 or mounted under api.py at /dashboard/.
const API = {
  status:     "api/status",
  ingestion:  "api/ingestion",
  alerts:     "api/alerts?limit=20",
};

function apiUrl(path) {
  // Preserve any mount prefix — e.g. if mounted at /dashboard, location.pathname
  // is "/dashboard/" and we want "/dashboard/api/status".
  const base = window.location.pathname.replace(/\/$/, "");
  return `${base}/${path}`;
}

async function jsonFetch(path) {
  try {
    const resp = await fetch(apiUrl(path), { cache: "no-store" });
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

// ---------------------------------------------------------------------------
// Renderers
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

function renderIngestion(data) {
  const body = document.getElementById("ingestion-body");
  if (data.__error) {
    body.innerHTML = `<div class="error">${escapeHtml(data.__error)}</div>`;
    return;
  }

  // Normalize: either .sla_report (from /health/detailed) or .sources (DB fallback).
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
// Helpers
// ---------------------------------------------------------------------------

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

// ---------------------------------------------------------------------------
// Main loop
// ---------------------------------------------------------------------------

async function refresh() {
  const [status, ingestion, alerts] = await Promise.all([
    jsonFetch(API.status),
    jsonFetch(API.ingestion),
    jsonFetch(API.alerts),
  ]);

  renderState(status);
  renderIngestion(ingestion);
  renderAlerts(alerts);

  setLastRefresh();
}

refresh();
setInterval(refresh, REFRESH_MS);
