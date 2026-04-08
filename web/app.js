const state = { defaults: null, runs: [], selectedRunId: null, detail: null, stream: null };
const intervalOptions = ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d"];
const numericFields = new Set([
  "starting_capital_twd",
  "check_interval_sec",
  "rsi_entry_low",
  "rsi_entry_high",
  "rsi_exit_high",
  "anti_chase_pct",
  "stop_loss_pct",
  "trail_trigger_pct",
  "trail_stop_pct",
  "ema_trend_period",
  "ema_signal_period",
  "rsi_period",
  "macd_fast",
  "macd_slow",
  "macd_signal_line",
]);

document.addEventListener("DOMContentLoaded", async () => {
  await Promise.all([loadDefaults(), refreshRuns()]);
  bindActions();
  toggleHistoricalFields();
  connectStream();
});

function bindActions() {
  const form = document.getElementById("run-form");
  form.addEventListener("submit", submitRunForm);
  document.getElementById("stop-run-button").addEventListener("click", () => runAction(stopSelectedRun));
  document.getElementById("import-log-button").addEventListener("click", () => runAction(importLog));
  document.getElementById("data-source").addEventListener("change", toggleHistoricalFields);
}

async function loadDefaults() {
  const defaults = await request("/api/config/defaults");
  state.defaults = defaults;
  const form = document.getElementById("run-form");
  ["trend_interval", "signal_interval", "historical_base_interval"].forEach((name) => {
    const field = form.elements[name];
    if (field) field.innerHTML = intervalOptions.map((value) => `<option value="${value}">${value}</option>`).join("");
  });
  Object.entries(defaults).forEach(([key, value]) => {
    if (form.elements[key] && form.elements[key].type !== "file") form.elements[key].value = value;
  });
  form.elements.data_source.value = "live";
  form.elements.historical_base_interval.value = defaults.signal_interval;
}

async function refreshRuns() {
  state.runs = await request("/api/runs");
  if (!state.selectedRunId && state.runs.length) state.selectedRunId = state.runs[0].id;
  renderRunList();
  if (state.selectedRunId) await selectRun(state.selectedRunId);
  else renderEmptyDashboard();
}

async function selectRun(runId) {
  state.selectedRunId = runId;
  state.detail = await request(`/api/runs/${runId}`);
  renderRunList();
  renderDashboard();
}

function toggleHistoricalFields() {
  const historical = document.getElementById("data-source").value === "historical";
  document.getElementById("historical-fields").classList.toggle("hidden", !historical);
  document.getElementById("historical-file").required = historical;
  document.getElementById("historical-base-interval").required = historical;
}

async function submitRunForm(event) {
  event.preventDefault();
  await runAction(async () => {
    const form = event.currentTarget;
    const mode = form.elements.data_source.value;
    if (mode === "historical") {
      await submitHistoricalRun(form);
    } else {
      await submitLiveRun(form);
    }
    await refreshRuns();
  });
}

async function submitLiveRun(form) {
  const payload = {};
  const formData = new FormData(form);
  formData.forEach((value, key) => {
    if (key === "data_source" || key === "historical_base_interval" || key === "file" || value === "") return;
    payload[key] = numericFields.has(key) ? Number(value) : value;
  });
  const run = await request("/api/runs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  state.selectedRunId = run.id;
}

async function submitHistoricalRun(form) {
  const payload = new FormData(form);
  const response = await fetch("/api/runs/historical", { method: "POST", body: payload });
  const body = await response.json().catch(() => ({ detail: response.statusText }));
  if (!response.ok) {
    throw new Error(formatError(body.detail));
  }
  state.selectedRunId = body.id;
}

async function stopSelectedRun() {
  const active = state.runs.find((run) => run.id === state.selectedRunId && run.status === "running");
  if (!active) return;
  await request(`/api/runs/${active.id}/stop`, { method: "POST" });
  await refreshRuns();
}

async function importLog() {
  await request("/api/import/log", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({}),
  });
  await refreshRuns();
}

function connectStream() {
  state.stream = new EventSource("/api/stream");
  state.stream.addEventListener("run_event", async () => {
    try {
      await refreshRuns();
    } catch (error) {
      console.error(error);
    }
  });
}

function renderDashboard() {
  if (!state.detail) return renderEmptyDashboard();
  renderStatusStrip(state.detail.run, state.detail.ticks);
  renderSummaryCards(state.detail.run.summary);
  renderCharts(state.detail);
  renderTimeline(state.detail.ticks);
  renderTradeTable(state.detail.trades);
}

function renderEmptyDashboard() {
  const empty = document.getElementById("empty-state").content.firstElementChild.outerHTML;
  ["status-strip", "summary-grid", "equity-chart", "price-chart", "rsi-chart", "macd-chart", "timeline", "trade-table"].forEach((id) => {
    document.getElementById(id).innerHTML = empty;
  });
}

function renderStatusStrip(run, ticks) {
  const latestTick = ticks[ticks.length - 1];
  const cards = [
    ["狀態", run.status, `Run ID ${run.id.slice(0, 8)}`],
    ["資料來源", run.config.data_source === "historical" ? "歷史回放" : "即時模擬", run.config.historical_source_filename || run.config.symbol],
    ["最新訊號", latestTick?.signal?.action ?? "n/a", latestTick?.signal?.reason ?? "暫無訊號"],
    ["最新價格", formatMoney(run.summary.last_price_twd), "TWD 顯示值"],
  ];
  if (run.config.data_source === "historical") {
    cards.push([
      "播放進度",
      formatPlayback(latestTick?.playback_index, latestTick?.playback_total),
      formatDate(latestTick?.market_timestamp),
    ]);
  } else {
    cards.push(["最後更新", formatDate(getTickTime(latestTick) || run.started_at), "系統事件時間"]);
  }
  document.getElementById("status-strip").innerHTML = cards
    .map(([label, value, meta]) => `<div class="status-card"><div class="metric-label">${label}</div><strong>${value}</strong><div class="status-meta">${meta}</div></div>`)
    .join("");
}

function renderSummaryCards(summary) {
  const cards = [
    ["目前淨值", formatMoney(summary.current_value_twd)],
    ["損益", formatSignedMoney(summary.pnl_twd)],
    ["報酬率", formatPercent(summary.pnl_pct)],
    ["最大回撤", formatPercent(summary.max_drawdown_pct)],
    ["總手續費", formatMoney(summary.total_fee_twd)],
    ["交易次數", String(summary.trade_count ?? 0)],
  ];
  document.getElementById("summary-grid").innerHTML = cards
    .map(([label, value]) => `<div class="summary-card"><div class="metric-label">${label}</div><strong>${value}</strong></div>`)
    .join("");
}

function renderRunList() {
  const host = document.getElementById("run-list");
  if (!state.runs.length) {
    host.innerHTML = document.getElementById("empty-state").content.firstElementChild.outerHTML;
    return;
  }
  host.innerHTML = state.runs
    .map((run) => `
      <div class="run-item ${run.id === state.selectedRunId ? "active" : ""}">
        <button type="button" data-run-id="${run.id}">
          <div class="run-meta">
            <span class="badge">${run.status}</span>
            <span class="badge">${run.config.data_source}</span>
            ${run.legacy_imported ? '<span class="badge">legacy</span>' : ""}
            ${run.incomplete ? '<span class="badge">incomplete</span>' : ""}
          </div>
          <h3>${formatDate(run.started_at)}</h3>
          <div class="run-meta">${run.config.symbol} · PnL ${formatSignedMoney(run.summary.pnl_twd)} · 勝率 ${formatPercent(run.summary.win_rate_pct)}</div>
        </button>
      </div>
    `)
    .join("");
  host.querySelectorAll("button[data-run-id]").forEach((button) => button.addEventListener("click", () => runAction(() => selectRun(button.dataset.runId))));
}

function renderTimeline(ticks) {
  const host = document.getElementById("timeline");
  if (!ticks.length) {
    host.innerHTML = document.getElementById("empty-state").content.firstElementChild.outerHTML;
    return;
  }
  host.innerHTML = ticks.slice(-20).reverse().map((tick) => `
    <article class="timeline-item">
      <h3>Tick #${tick.tick_index} · ${tick.signal?.action ?? (tick.status === "error" ? "ERROR" : "n/a")}</h3>
      <p>${formatDate(getTickTime(tick))} · 價格 ${formatMoney(tick.price_twd)} · RSI ${formatNumber(tick.indicators?.rsi)}</p>
      <p>${escapeHtml(tick.signal?.reason ?? tick.error ?? "暫無說明")}</p>
    </article>
  `).join("");
}

function renderTradeTable(trades) {
  const host = document.getElementById("trade-table");
  if (!trades.length) {
    host.innerHTML = document.getElementById("empty-state").content.firstElementChild.outerHTML;
    return;
  }
  host.innerHTML = `
    <table class="trade-table">
      <thead><tr><th>時間</th><th>方向</th><th>價格</th><th>BTC</th><th>手續費</th><th>原因</th></tr></thead>
      <tbody>
        ${trades.slice().reverse().map((trade) => `
          <tr>
            <td>${formatDate(getTradeTime(trade))}</td>
            <td class="${trade.action === "BUY" ? "positive" : "negative"}">${trade.action}</td>
            <td>${formatMoney(trade.price_twd)}</td>
            <td>${formatNumber(trade.btc_amount, 6)}</td>
            <td>${formatMoney(trade.fee_twd)}</td>
            <td>${escapeHtml(trade.reason)}</td>
          </tr>
        `).join("")}
      </tbody>
    </table>
  `;
}

function renderCharts(detail) {
  const ticks = detail.ticks.filter((tick) => tick.price_twd != null);
  renderLineChart("equity-chart", ticks, [{ key: "portfolio.total_value_twd", color: "#e9723d" }]);
  renderLineChart(
    "price-chart",
    ticks,
    [
      { key: "price_twd", color: "#0d8f8b" },
      { key: "indicators.ema20_twd", color: "#e9723d" },
      { key: "indicators.ema200_twd", color: "#17212d" },
    ],
    detail.trades
  );
  renderLineChart("rsi-chart", ticks, [{ key: "indicators.rsi", color: "#bf4b18" }], [], { min: 0, max: 100 });
  renderBarChart("macd-chart", ticks, "indicators.macd_hist", "#197c5b", "#c24a3a");
}

function renderLineChart(containerId, ticks, seriesDefinitions, trades = [], fixedDomain = null) {
  const host = document.getElementById(containerId);
  if (!ticks.length) {
    host.innerHTML = document.getElementById("empty-state").content.firstElementChild.outerHTML;
    return;
  }
  const width = 860;
  const height = 230;
  const padding = 24;
  const allValues = seriesDefinitions.flatMap((series) => ticks.map((tick) => getValue(tick, series.key)).filter((value) => value != null));
  const min = fixedDomain?.min ?? Math.min(...allValues);
  const max = fixedDomain?.max ?? Math.max(...allValues);
  const span = max - min || 1;
  const pointMap = new Map(ticks.map((tick, index) => [getTickTime(tick), index]));
  const chartSeries = seriesDefinitions.map((series) => {
    const points = ticks.map((tick, index) => {
      const value = getValue(tick, series.key);
      if (value == null) return null;
      return `${scaleX(index, ticks.length, width, padding)},${scaleY(value, min, span, height, padding)}`;
    }).filter(Boolean).join(" ");
    return `<polyline fill="none" stroke="${series.color}" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" points="${points}" />`;
  }).join("");
  const markers = trades.map((trade) => {
    const index = pointMap.get(getTradeTime(trade));
    if (index == null) return "";
    return `<circle cx="${scaleX(index, ticks.length, width, padding)}" cy="${scaleY(trade.price_twd ?? 0, min, span, height, padding)}" r="5" fill="${trade.action === "BUY" ? "#197c5b" : "#c24a3a"}" stroke="white" stroke-width="2" />`;
  }).join("");
  host.innerHTML = `<svg class="chart-svg" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none"><rect x="0" y="0" width="${width}" height="${height}" rx="18" fill="rgba(255,255,255,0.42)"></rect>${chartSeries}${markers}</svg>`;
}

function renderBarChart(containerId, ticks, key, positiveColor, negativeColor) {
  const host = document.getElementById(containerId);
  const values = ticks.map((tick) => getValue(tick, key)).filter((value) => value != null);
  if (!values.length) {
    host.innerHTML = document.getElementById("empty-state").content.firstElementChild.outerHTML;
    return;
  }
  const width = 860;
  const height = 230;
  const padding = 24;
  const min = Math.min(...values, 0);
  const max = Math.max(...values, 0);
  const span = max - min || 1;
  const zeroY = scaleY(0, min, span, height, padding);
  const barWidth = Math.max(4, (width - padding * 2) / Math.max(values.length, 1) - 2);
  const bars = values.map((value, index) => {
    const x = scaleX(index, values.length, width, padding);
    const top = Math.min(scaleY(value, min, span, height, padding), zeroY);
    const barHeight = Math.abs(zeroY - scaleY(value, min, span, height, padding));
    return `<rect x="${x - barWidth / 2}" y="${top}" width="${barWidth}" height="${Math.max(barHeight, 2)}" fill="${value >= 0 ? positiveColor : negativeColor}" rx="2" />`;
  }).join("");
  host.innerHTML = `<svg class="chart-svg" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none"><rect x="0" y="0" width="${width}" height="${height}" rx="18" fill="rgba(255,255,255,0.42)"></rect><line x1="${padding}" y1="${zeroY}" x2="${width - padding}" y2="${zeroY}" stroke="rgba(23,33,45,0.16)" />${bars}</svg>`;
}

function scaleX(index, length, width, padding) { return length <= 1 ? width / 2 : padding + (index / (length - 1)) * (width - padding * 2); }
function scaleY(value, min, span, height, padding) { return height - padding - ((value - min) / span) * (height - padding * 2); }
function getValue(target, path) { return path.split(".").reduce((current, segment) => current?.[segment], target); }
function formatMoney(value) { return value == null || Number.isNaN(Number(value)) ? "n/a" : new Intl.NumberFormat("zh-TW", { style: "currency", currency: "TWD", maximumFractionDigits: 2 }).format(value); }
function formatSignedMoney(value) { return value == null || Number.isNaN(Number(value)) ? "n/a" : `${value >= 0 ? "+" : "-"}${formatMoney(Math.abs(value))}`; }
function formatPercent(value) { return value == null || Number.isNaN(Number(value)) ? "n/a" : `${value >= 0 ? "+" : ""}${Number(value).toFixed(2)}%`; }
function formatNumber(value, digits = 2) { return value == null || Number.isNaN(Number(value)) ? "n/a" : Number(value).toFixed(digits); }
function formatDate(value) { return value ? new Intl.DateTimeFormat("zh-TW", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(new Date(value)) : "n/a"; }
function formatPlayback(index, total) { return index && total ? `${index} / ${total}` : "n/a"; }
function getTickTime(tick) { return tick?.market_timestamp ?? tick?.timestamp ?? null; }
function getTradeTime(trade) { return trade?.market_timestamp ?? trade?.timestamp ?? null; }
function escapeHtml(value) { return String(value).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;"); }

async function runAction(fn) {
  try {
    await fn();
  } catch (error) {
    window.alert(error.message || "Request failed");
  }
}

function formatError(detail) {
  if (Array.isArray(detail)) return detail.map((item) => item.msg || JSON.stringify(item)).join("\n");
  return detail || "Request failed";
}

async function request(url, options = {}) {
  const response = await fetch(url, options);
  const payload = await response.json().catch(() => ({ detail: response.statusText }));
  if (!response.ok) {
    throw new Error(formatError(payload.detail));
  }
  return payload;
}
