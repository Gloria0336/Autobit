const ANALYSIS_API_KEY_STORAGE = "autobit.openrouter.apiKey";
const ANALYSIS_MODEL_STORAGE = "autobit.openrouter.model";

const state = {
  defaults: null,
  analysisConfig: null,
  runs: [],
  selectedRunId: null,
  detail: null,
  stream: null,
  reportPayload: null,
  reportAnalysis: null,
  analysisStatus: null,
  timelinePage: 0,
  refreshTimer: null,
  refreshInFlight: false,
  runActionState: "idle",
  analysisInFlight: false,
};

const intervalOptions = ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d"];
const timelinePageSize = 5;
const DASHBOARD_REFRESH_MS = 10_000;
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
  bindActions();
  await Promise.all([loadDefaults(), loadAnalysisConfig(), refreshRuns()]);
  toggleHistoricalFields();
  renderReportPanel();
  connectStream();
  startAutoRefresh();
});

function bindActions() {
  const form = document.getElementById("run-form");
  const apiKeyInput = document.getElementById("report-api-key-input");
  const modelInput = document.getElementById("report-model-input");

  form.addEventListener("submit", submitRunForm);
  document.getElementById("stop-run-button").addEventListener("click", () => runAction(stopSelectedRun));
  document.getElementById("import-log-button").addEventListener("click", () => runAction(importLog));
  document.getElementById("data-source").addEventListener("change", toggleHistoricalFields);
  document.getElementById("historical-source-mode").addEventListener("change", toggleHistoricalFields);
  document.getElementById("generate-report-button").addEventListener("click", () => runAction(generateReport));
  document.getElementById("copy-prompt-button").addEventListener("click", () => runAction(copyPrompt));
  document.getElementById("download-report-json-button").addEventListener("click", () => runAction(downloadReportJson));
  document.getElementById("download-report-md-button").addEventListener("click", () => runAction(downloadReportMarkdown));
  document.getElementById("analyze-report-button").addEventListener("click", () => runAction(analyzeReport));
  document.getElementById("test-analysis-button").addEventListener("click", () => runAction(testAnalysisConnection));
  apiKeyInput.addEventListener("input", persistAnalysisPreferences);
  modelInput.addEventListener("input", () => {
    persistAnalysisPreferences();
    renderModelSuggestions();
  });
  modelInput.addEventListener("focus", renderModelSuggestions);
  modelInput.addEventListener("blur", () => window.setTimeout(renderModelSuggestions, 120));
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
  form.elements.historical_source_mode.value = "binance_api";
  form.elements.historical_base_interval.value = defaults.signal_interval;
  setDefaultHistoricalRange(form);
}

async function loadAnalysisConfig() {
  state.analysisConfig = await request("/api/config/analysis");
  const apiKeyInput = document.getElementById("report-api-key-input");
  const modelInput = document.getElementById("report-model-input");
  apiKeyInput.value = window.localStorage.getItem(ANALYSIS_API_KEY_STORAGE) || "";
  modelInput.value = window.localStorage.getItem(ANALYSIS_MODEL_STORAGE) || state.analysisConfig.default_model || "";
  modelInput.placeholder = state.analysisConfig.default_model || "例如 openai/gpt-4.1-mini";
  document.getElementById("report-provider-badge").textContent = state.analysisConfig.provider || "openrouter";
  populateModelDatalist();
  renderModelSuggestions();
  renderAnalysisStatus();
}

function populateModelDatalist() {
  const datalist = document.getElementById("model-options");
  const models = state.analysisConfig?.recommended_models || [];
  datalist.innerHTML = models.map((model) => `<option value="${escapeAttribute(model)}"></option>`).join("");
}

function renderModelSuggestions() {
  const host = document.getElementById("model-suggestion-list");
  const modelInput = document.getElementById("report-model-input");
  const query = modelInput.value.trim().toLowerCase();
  const models = state.analysisConfig?.recommended_models || [];
  const filtered = models.filter((model) => !query || model.toLowerCase().includes(query)).slice(0, 8);

  if (!filtered.length) {
    host.innerHTML = "";
    return;
  }

  host.innerHTML = filtered.map((model) => `<button type="button" class="model-suggestion-item" data-model="${escapeAttribute(model)}">${escapeHtml(model)}</button>`).join("");
  host.querySelectorAll("[data-model]").forEach((button) => {
    button.addEventListener("click", () => {
      modelInput.value = button.dataset.model || "";
      persistAnalysisPreferences();
      renderModelSuggestions();
    });
  });
}

function persistAnalysisPreferences() {
  const apiKey = document.getElementById("report-api-key-input").value.trim();
  const model = document.getElementById("report-model-input").value.trim();
  if (apiKey) window.localStorage.setItem(ANALYSIS_API_KEY_STORAGE, apiKey);
  else window.localStorage.removeItem(ANALYSIS_API_KEY_STORAGE);
  if (model) window.localStorage.setItem(ANALYSIS_MODEL_STORAGE, model);
  else window.localStorage.removeItem(ANALYSIS_MODEL_STORAGE);
}

function getAnalysisCredentials() {
  const apiKey = document.getElementById("report-api-key-input").value.trim();
  const model = document.getElementById("report-model-input").value.trim() || (state.analysisConfig?.default_model || "");
  return { api_key: apiKey || null, model: model || null };
}

async function refreshRuns() {
  state.runs = await request("/api/runs");
  if (!state.runs.length) {
    state.selectedRunId = null;
    state.detail = null;
    resetReportState();
    renderRunList();
    renderEmptyDashboard();
    return;
  }
  if (!state.selectedRunId || !state.runs.some((run) => run.id === state.selectedRunId)) state.selectedRunId = state.runs[0].id;
  renderRunList();
  renderSimulationButtons();
  await selectRun(state.selectedRunId);
}

function startAutoRefresh() {
  if (state.refreshTimer) window.clearInterval(state.refreshTimer);
  state.refreshTimer = window.setInterval(() => {
    void refreshRunsSafely();
  }, DASHBOARD_REFRESH_MS);
}

async function refreshRunsSafely() {
  if (state.refreshInFlight) return;
  state.refreshInFlight = true;
  try {
    await refreshRuns();
  } catch (error) {
    console.error(error);
  } finally {
    state.refreshInFlight = false;
  }
}

async function selectRun(runId) {
  const changed = state.selectedRunId !== runId;
  state.selectedRunId = runId;
  if (changed) {
    resetReportState();
    state.timelinePage = 0;
  }
  state.detail = await request(`/api/runs/${runId}`);
  renderRunList();
  renderDashboard();
}

function resetReportState() {
  state.reportPayload = null;
  state.reportAnalysis = null;
}

function toggleHistoricalFields() {
  const historical = document.getElementById("data-source").value === "historical";
  const sourceMode = document.getElementById("historical-source-mode").value;
  const useBinanceApi = historical && sourceMode === "binance_api";
  const useCsvUpload = historical && sourceMode === "csv_upload";
  document.getElementById("historical-fields").classList.toggle("hidden", !historical);
  document.getElementById("historical-binance-fields").classList.toggle("hidden", !useBinanceApi);
  document.getElementById("historical-upload-fields").classList.toggle("hidden", !useCsvUpload);
  document.getElementById("historical-file").required = useCsvUpload;
  document.getElementById("historical-start-at").required = useBinanceApi;
  document.getElementById("historical-end-at").required = useBinanceApi;
  document.getElementById("historical-base-interval").required = historical;
}

async function submitRunForm(event) {
  event.preventDefault();
  if (state.runActionState !== "idle") return;
  state.runActionState = "starting";
  renderSimulationButtons();
  await runAction(async () => {
    const form = event.currentTarget;
    if (form.elements.data_source.value === "historical") await submitHistoricalRun(form);
    else await submitLiveRun(form);
    await refreshRuns();
  }, {
    onFinally: () => {
      state.runActionState = "idle";
      renderSimulationButtons();
    },
  });
}

async function submitLiveRun(form) {
  const payload = {};
  const formData = new FormData(form);
  formData.forEach((value, key) => {
    if (
      key === "data_source"
      || key === "historical_base_interval"
      || key === "historical_source_mode"
      || key === "historical_start_at"
      || key === "historical_end_at"
      || key === "file"
      || value === ""
    ) return;
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
  const payload = new FormData();
  const formData = new FormData(form);
  const sourceMode = form.elements.historical_source_mode.value;
  formData.forEach((value, key) => {
    if (key === "file" && sourceMode !== "csv_upload") return;
    if ((key === "historical_start_at" || key === "historical_end_at") && sourceMode !== "binance_api") return;
    if (value instanceof File && !value.name) return;
    if (value === "") return;
    if (key === "historical_start_at" || key === "historical_end_at") {
      payload.append(key, toUtcIsoString(String(value)));
      return;
    }
    payload.append(key, value);
  });
  const response = await fetch("/api/runs/historical", { method: "POST", body: payload });
  const body = await response.json().catch(() => ({ detail: response.statusText }));
  if (!response.ok) throw new Error(formatError(body.detail));
  state.selectedRunId = body.id;
}

function setDefaultHistoricalRange(form) {
  const end = new Date();
  end.setSeconds(0, 0);
  const start = new Date(end.getTime() - 30 * 24 * 60 * 60 * 1000);
  form.elements.historical_start_at.value = toDateTimeLocalValue(start);
  form.elements.historical_end_at.value = toDateTimeLocalValue(end);
}

function toDateTimeLocalValue(date) {
  const offsetMs = date.getTimezoneOffset() * 60 * 1000;
  return new Date(date.getTime() - offsetMs).toISOString().slice(0, 16);
}

function toUtcIsoString(localValue) {
  const parsed = new Date(localValue);
  return Number.isNaN(parsed.getTime()) ? localValue : parsed.toISOString();
}

async function stopSelectedRun() {
  const active = state.runs.find((run) => run.id === state.selectedRunId && run.status === "running");
  if (!active) return;
  if (state.runActionState !== "idle") return;
  state.runActionState = "stopping";
  renderSimulationButtons();
  try {
    await request(`/api/runs/${active.id}/stop`, { method: "POST" });
    await refreshRuns();
  } finally {
    state.runActionState = "idle";
    renderSimulationButtons();
  }
}

async function importLog() {
  await request("/api/import/log", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({}),
  });
  await refreshRuns();
}

function applyReportPayload(payload, { invalidateAnalysis = false } = {}) {
  state.reportPayload = payload;
  if (invalidateAnalysis) {
    state.reportAnalysis = null;
    state.analysisStatus = {
      tone: "warning",
      text: "報告已更新",
      detail: "已重新抓取最新 run 資料；如需最新 AI 建議，請再按一次「AI 分析」。",
    };
  }
}

async function ensureReportLoaded({ forceReload = false, invalidateAnalysis = false } = {}) {
  if (!state.selectedRunId) throw new Error("請先選擇一個 run");
  if (forceReload || !state.reportPayload) {
    const payload = await request(`/api/runs/${state.selectedRunId}/report`);
    applyReportPayload(payload, { invalidateAnalysis });
  }
  const modelInput = document.getElementById("report-model-input");
  if (!modelInput.value && state.reportPayload.default_model) {
    modelInput.value = state.reportPayload.default_model;
    persistAnalysisPreferences();
    renderModelSuggestions();
  }
  renderReportPanel();
  return state.reportPayload;
}

async function generateReport() {
  await ensureReportLoaded({ forceReload: true, invalidateAnalysis: true });
}

async function copyPrompt() {
  const payload = await ensureReportLoaded();
  if (!navigator.clipboard) throw new Error("目前瀏覽器不支援剪貼簿複製");
  await navigator.clipboard.writeText(payload.prompt);
  window.alert("AI Prompt 已複製到剪貼簿");
}

async function downloadReportJson() {
  const payload = await ensureReportLoaded();
  downloadFile(`${state.selectedRunId}-report.json`, JSON.stringify(payload, null, 2), "application/json");
}

async function downloadReportMarkdown() {
  const payload = await ensureReportLoaded();
  downloadFile(`${state.selectedRunId}-report.md`, payload.markdown, "text/markdown;charset=utf-8");
}

async function testAnalysisConnection() {
  const credentials = getAnalysisCredentials();
  const result = await request("/api/analysis/test", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Title": "Autobit Web Dashboard" },
    body: JSON.stringify(credentials),
  });
  state.analysisStatus = {
    tone: "success",
    text: `${result.provider} ${result.model} 測試成功`,
    detail: result.message,
  };
  renderAnalysisStatus();
}

async function analyzeReport() {
  await ensureReportLoaded({ forceReload: true, invalidateAnalysis: true });
  if (state.analysisInFlight) return;
  const credentials = getAnalysisCredentials();
  state.analysisInFlight = true;
  state.analysisStatus = {
    tone: "loading",
    text: "AI 分析中",
    detail: "正在等待模型回應，分析完成或出錯後會恢復按鈕。",
  };
  renderAnalysisStatus();
  renderActionButtons();
  try {
    state.reportAnalysis = await request(`/api/runs/${state.selectedRunId}/report/analyze`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Title": "Autobit Web Dashboard" },
      body: JSON.stringify({
        api_key: credentials.api_key,
        model: credentials.model,
        language: "zh-TW",
        max_observations: 5,
        max_recommendations: 5,
      }),
    });
    state.reportPayload = {
      ...state.reportPayload,
      report: state.reportAnalysis.report,
      markdown: state.reportAnalysis.markdown,
    };
    state.analysisStatus = {
      tone: "success",
      text: `AI 分析完成: ${state.reportAnalysis.model}`,
      detail: state.reportAnalysis.parsing_error ? `解析提醒: ${state.reportAnalysis.parsing_error}` : "已成功取得 AI 回應",
    };
  } catch (error) {
    state.analysisStatus = {
      tone: "error",
      text: "AI 分析失敗",
      detail: error.message || "Request failed",
    };
    renderAnalysisStatus();
    renderReportPanel();
    throw error;
  } finally {
    state.analysisInFlight = false;
    renderActionButtons();
  }
  renderAnalysisStatus();
  renderReportPanel();
}

function connectStream() {
  state.stream = new EventSource("/api/stream");
  state.stream.addEventListener("run_event", async () => {
    await refreshRunsSafely();
  });
}

function renderDashboard() {
  if (!state.detail) return renderEmptyDashboard();
  renderSimulationButtons();
  renderStatusStrip(state.detail.run, state.detail.ticks);
  renderSummaryCards(state.detail.run.summary);
  renderCharts(state.detail);
  renderTimelinePaginated(state.detail.ticks);
  renderTradeTable(state.detail.trades);
  renderReportPanel();
}

function renderEmptyDashboard() {
  const empty = document.getElementById("empty-state").content.firstElementChild.outerHTML;
  ["status-strip", "summary-grid", "equity-chart", "price-chart", "rsi-chart", "macd-chart", "timeline", "trade-table", "report-content"].forEach((id) => {
    document.getElementById(id).innerHTML = empty;
  });
  renderSimulationButtons();
  renderActionButtons();
}

function getSelectedRun() {
  return state.runs.find((run) => run.id === state.selectedRunId) ?? null;
}

function renderSimulationButtons() {
  const startButton = document.getElementById("start-run-button");
  const stopButton = document.getElementById("stop-run-button");
  if (!startButton || !stopButton) return;

  const runningRun = state.runs.find((run) => run.status === "running");
  const selectedRun = getSelectedRun();
  const selectedIsRunning = selectedRun?.status === "running";
  const busy = state.runActionState !== "idle";

  startButton.disabled = busy || Boolean(runningRun);
  startButton.classList.toggle("is-busy", state.runActionState === "starting");
  startButton.dataset.toggleState = runningRun ? "inactive" : "active";
  startButton.textContent = state.runActionState === "starting" ? "開始模擬中..." : (runningRun ? "開始模擬（目前已有執行中）" : "開始模擬");

  stopButton.disabled = busy || !selectedIsRunning;
  stopButton.classList.toggle("is-busy", state.runActionState === "stopping");
  stopButton.dataset.toggleState = selectedIsRunning ? "active" : "inactive";
  stopButton.textContent = state.runActionState === "stopping" ? "結束模擬中..." : "結束模擬";
}

function renderStatusStrip(run, ticks) {
  const latestTick = ticks[ticks.length - 1];
  const cards = [
    ["狀態", run.status, `Run ID ${run.id.slice(0, 8)}`],
    ["資料來源", run.config.data_source === "historical" ? "歷史回放" : "即時模擬", run.config.historical_source_filename || run.config.symbol],
    ["最新訊號", latestTick?.signal?.action ?? "n/a", latestTick?.signal?.reason ?? "尚無訊號"],
    ["最新價格", formatMoney(run.summary.last_price_twd), `${run.config.symbol} x ${run.config.fx_rate_base}/${run.config.fx_rate_quote}`],
  ];
  if (run.config.data_source === "historical") cards.push(["回放進度", formatPlayback(latestTick?.playback_index, latestTick?.playback_total), formatDate(latestTick?.market_timestamp)]);
  else cards.push(["最後更新", formatDate(getTickTime(latestTick) || run.started_at), "Live feed"]);
  document.getElementById("status-strip").innerHTML = cards.map(([label, value, meta]) => `<div class="status-card"><div class="metric-label">${escapeHtml(label)}</div><strong>${escapeHtml(value)}</strong><div class="status-meta">${escapeHtml(meta)}</div></div>`).join("");
}

function renderSummaryCards(summary) {
  const cards = [
    ["總資產", formatMoney(summary.current_value_twd)],
    ["損益", formatSignedMoney(summary.pnl_twd)],
    ["報酬率", formatPercent(summary.pnl_pct)],
    ["最大回撤", formatPercent(summary.max_drawdown_pct)],
    ["手續費", formatMoney(summary.total_fee_twd)],
    ["交易次數", String(summary.trade_count ?? 0)],
  ];
  document.getElementById("summary-grid").innerHTML = cards.map(([label, value]) => `<div class="summary-card"><div class="metric-label">${escapeHtml(label)}</div><strong>${value}</strong></div>`).join("");
}

function renderRunList() {
  const host = document.getElementById("run-list");
  if (!state.runs.length) {
    host.innerHTML = document.getElementById("empty-state").content.firstElementChild.outerHTML;
    return;
  }
  host.innerHTML = state.runs.map((run) => `
    <div class="run-item ${run.id === state.selectedRunId ? "active" : ""}">
      <button type="button" data-run-id="${run.id}">
        <div class="run-meta">
          <span class="badge">${escapeHtml(run.status)}</span>
          <span class="badge">${escapeHtml(run.config.data_source)}</span>
          ${run.legacy_imported ? '<span class="badge">legacy</span>' : ""}
          ${run.incomplete ? '<span class="badge">incomplete</span>' : ""}
        </div>
        <h3>${formatDate(run.started_at)}</h3>
        <div class="run-meta">${escapeHtml(run.config.symbol)} | PnL ${formatSignedMoney(run.summary.pnl_twd)} | 勝率 ${formatPercent(run.summary.win_rate_pct)}</div>
      </button>
    </div>
  `).join("");
  host.querySelectorAll("button[data-run-id]").forEach((button) => button.addEventListener("click", () => runAction(() => selectRun(button.dataset.runId))));
}

function renderTradeTable(trades) {
  const host = document.getElementById("trade-table");
  if (!trades.length) {
    host.innerHTML = document.getElementById("empty-state").content.firstElementChild.outerHTML;
    return;
  }
  host.innerHTML = `
    <table class="trade-table">
      <thead><tr><th>時間</th><th>動作</th><th>價格</th><th>BTC</th><th>手續費</th><th>理由</th></tr></thead>
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

function renderTimelinePaginated(ticks) {
  const host = document.getElementById("timeline");
  if (!ticks.length) {
    host.innerHTML = document.getElementById("empty-state").content.firstElementChild.outerHTML;
    return;
  }

  const orderedTicks = ticks.slice().reverse();
  const totalPages = Math.max(1, Math.ceil(orderedTicks.length / timelinePageSize));
  state.timelinePage = Math.min(state.timelinePage, totalPages - 1);
  const startIndex = state.timelinePage * timelinePageSize;
  const pageTicks = orderedTicks.slice(startIndex, startIndex + timelinePageSize);

  host.innerHTML = `
    <div class="timeline-toolbar">
      <span class="timeline-page-indicator">Page ${state.timelinePage + 1} / ${totalPages}</span>
      <div class="timeline-actions">
        <button type="button" class="ghost timeline-page-button" data-page-action="newer" ${state.timelinePage === 0 ? "disabled" : ""}>Newer</button>
        <button type="button" class="ghost timeline-page-button" data-page-action="older" ${state.timelinePage >= totalPages - 1 ? "disabled" : ""}>Older</button>
      </div>
    </div>
    <div class="timeline-list">
      ${pageTicks.map((tick) => `
        <article class="timeline-item">
          <h3>Tick #${tick.tick_index} | ${escapeHtml(tick.signal?.action ?? (tick.status === "error" ? "ERROR" : "n/a"))}</h3>
          <p>${formatDate(getTickTime(tick))} | 價格 ${formatMoney(tick.price_twd)} | RSI ${formatNumber(tick.indicators?.rsi)}</p>
          <p>${escapeHtml(tick.signal?.reason ?? tick.error ?? "無額外說明")}</p>
        </article>
      `).join("")}
    </div>
  `;

  host.querySelector('[data-page-action="newer"]')?.addEventListener("click", () => {
    if (state.timelinePage <= 0) return;
    state.timelinePage -= 1;
    renderTimelinePaginated(ticks);
  });
  host.querySelector('[data-page-action="older"]')?.addEventListener("click", () => {
    if (state.timelinePage >= totalPages - 1) return;
    state.timelinePage += 1;
    renderTimelinePaginated(ticks);
  });
}

function renderCharts(detail) {
  const ticks = detail.ticks.filter((tick) => tick.price_twd != null);
  renderLineChart("equity-chart", ticks, [{ key: "portfolio.total_value_twd", color: "#e9723d" }]);
  renderLineChart("price-chart", ticks, [{ key: "price_twd", color: "#0d8f8b" }, { key: "indicators.ema20_twd", color: "#e9723d" }, { key: "indicators.ema200_twd", color: "#17212d" }], detail.trades);
  renderLineChart("rsi-chart", ticks, [{ key: "indicators.rsi", color: "#bf4b18" }], [], { min: 0, max: 100 });
  renderBarChart("macd-chart", ticks, "indicators.macd_hist", "#197c5b", "#c24a3a");
}

function renderAnalysisStatus() {
  const host = document.getElementById("analysis-test-status");
  const status = state.analysisStatus;
  host.textContent = status?.text || "尚未測試連線";
  host.className = `report-status ${status ? `is-${status.tone}` : ""}`.trim();
  host.title = status?.detail || "";
}

function renderReportPanel() {
  renderAnalysisStatus();
  renderActionButtons();
  const host = document.getElementById("report-content");
  if (!state.detail) {
    host.innerHTML = document.getElementById("empty-state").content.firstElementChild.outerHTML;
    return;
  }
  if (!state.reportPayload) {
    host.innerHTML = `<div class="empty-state"><p>先選擇 run，接著按下「產生報告」即可建立 AI 分析素材。</p></div>`;
    return;
  }

  const report = state.reportPayload.report;
  const analysis = state.reportAnalysis;
  const observationCards = (report.fallback_observations || []).map((item) => `
    <article class="observation-card">
      <div class="observation-head">
        <span class="badge">${escapeHtml(item.category)}</span>
        <span class="badge">${escapeHtml(item.severity)}</span>
      </div>
      <h3>${escapeHtml(item.title)}</h3>
      <p>${escapeHtml(item.detail)}</p>
      ${item.evidence?.length ? `<div class="micro-list">${item.evidence.map((value) => `<span>${escapeHtml(value)}</span>`).join("")}</div>` : ""}
    </article>
  `).join("");

  const recommendations = (analysis?.recommendations || []).map((item) => `
    <article class="recommendation-card">
      <div class="recommendation-row"><strong>${escapeHtml(item.parameter)}</strong><span class="badge">${escapeHtml(item.confidence)}</span></div>
      <p>目前值 <code>${escapeHtml(String(item.current_value))}</code>，建議調整為 <code>${escapeHtml(String(item.suggested_value))}</code> (${escapeHtml(item.suggested_change)})</p>
      <p>${escapeHtml(item.reason)}</p>
      <p class="status-meta">預期效果: ${escapeHtml(item.expected_effect)}</p>
    </article>
  `).join("");

  host.innerHTML = `
    <div class="report-shell">
      <section class="report-summary-grid">
        <article class="summary-card"><div class="metric-label">Round Trips</div><strong>${report.trade_breakdown.round_trip_count}</strong></article>
        <article class="summary-card"><div class="metric-label">Fee / PnL</div><strong>${formatPercent(report.risk_diagnostics.fee_to_pnl_ratio_pct)}</strong></article>
        <article class="summary-card"><div class="metric-label">Entry Tightness</div><strong>${escapeHtml(report.strategy_recommendation_context.entry_filter_tightness)}</strong></article>
        <article class="summary-card"><div class="metric-label">Exit Efficiency</div><strong>${escapeHtml(report.strategy_recommendation_context.exit_efficiency)}</strong></article>
      </section>

      <section class="report-columns">
        <article class="report-block">
          <div class="card-head"><h3>Fallback 觀察</h3><p>即使 AI 尚未呼叫，也會先顯示本地策略診斷摘要。</p></div>
          <div class="observation-list">${observationCards || '<div class="empty-state"><p>目前沒有 fallback 觀察。</p></div>'}</div>
        </article>
        <article class="report-block">
          <div class="card-head"><h3>訊號診斷</h3><p>檢視 BUY / SELL / HOLD 訊號與停損統計。</p></div>
          <div class="report-kv">
            <div><span>BUY 訊號</span><strong>${report.signal_diagnostics.buy_signal_count}</strong></div>
            <div><span>SELL 訊號</span><strong>${report.signal_diagnostics.sell_signal_count}</strong></div>
            <div><span>HOLD 訊號</span><strong>${report.signal_diagnostics.hold_signal_count}</strong></div>
            <div><span>停損觸發</span><strong>${report.risk_diagnostics.stop_loss_trigger_count}</strong></div>
            <div><span>移動停利觸發</span><strong>${report.risk_diagnostics.trailing_stop_trigger_count}</strong></div>
            <div><span>主要 HOLD 原因</span><strong>${escapeHtml(report.strategy_recommendation_context.dominant_hold_reason || "n/a")}</strong></div>
          </div>
          <h4>Top HOLD Reasons</h4>
          ${formatReasonList(report.signal_diagnostics.top_hold_reasons)}
          <h4>Top SELL Reasons</h4>
          ${formatReasonList(report.signal_diagnostics.top_sell_reasons)}
        </article>
      </section>

      <section class="report-columns">
        <article class="report-block">
          <div class="card-head"><h3>Markdown 報告</h3><p>可直接貼給外部 AI 或保存為文字報告。</p></div>
          <pre class="report-pre">${escapeHtml(state.reportPayload.markdown)}</pre>
        </article>
        <article class="report-block">
          <div class="card-head"><h3>AI 原始回覆</h3><p>${analysis ? `模型: ${escapeHtml(analysis.model)}` : "尚未執行 AI 分析"}</p></div>
          ${analysis ? `<pre class="report-pre">${escapeHtml(analysis.ai_analysis_markdown)}</pre>` : '<div class="empty-state"><p>填入 API Key 並按下「AI 分析」後，這裡會顯示模型完整回覆。</p></div>'}
        </article>
      </section>

      <section class="report-columns">
        <article class="report-block">
          <div class="card-head"><h3>策略修正建議</h3><p>AI 會以現有策略參數為範圍，回傳可測試的調整方向。</p></div>
          <div class="recommendation-list">${recommendations || '<div class="empty-state"><p>尚未取得 AI 的策略建議。</p></div>'}</div>
          ${analysis?.parsing_error ? `<p class="report-warning">JSON 解析提醒: ${escapeHtml(analysis.parsing_error)}</p>` : ""}
        </article>
        <article class="report-block">
          <div class="card-head"><h3>下一輪測試清單</h3><p>整理下一次回測時可以優先驗證的重點。</p></div>
          ${formatStringList(analysis?.test_plan || [])}
        </article>
      </section>
    </div>
  `;
}

function renderActionButtons() {
  const analyzeButton = document.getElementById("analyze-report-button");
  if (analyzeButton) {
    analyzeButton.disabled = state.analysisInFlight;
    analyzeButton.classList.toggle("is-busy", state.analysisInFlight);
    analyzeButton.textContent = state.analysisInFlight ? "AI 分析中..." : "AI 分析";
  }
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

function formatReasonList(items) {
  if (!items?.length) return '<div class="empty-state"><p>沒有可顯示的統計。</p></div>';
  return `<div class="stack-list">${items.map((item) => `<div class="stack-row"><span>${escapeHtml(item.reason)}</span><strong>${item.count} / ${formatPercent(item.ratio_pct)}</strong></div>`).join("")}</div>`;
}

function formatStringList(items) {
  if (!items?.length) return '<div class="empty-state"><p>目前沒有下一輪測試建議。</p></div>';
  return `<ul class="plain-list">${items.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`;
}

function scaleX(index, length, width, padding) {
  return length <= 1 ? width / 2 : padding + (index / (length - 1)) * (width - padding * 2);
}

function scaleY(value, min, span, height, padding) {
  return height - padding - ((value - min) / span) * (height - padding * 2);
}

function getValue(target, path) {
  return path.split(".").reduce((current, segment) => current?.[segment], target);
}

function formatMoney(value) {
  return value == null || Number.isNaN(Number(value)) ? "n/a" : new Intl.NumberFormat("zh-TW", { style: "currency", currency: "TWD", maximumFractionDigits: 2 }).format(value);
}

function formatSignedMoney(value) {
  return value == null || Number.isNaN(Number(value)) ? "n/a" : `${value >= 0 ? "+" : "-"}${formatMoney(Math.abs(value))}`;
}

function formatPercent(value) {
  return value == null || Number.isNaN(Number(value)) ? "n/a" : `${value >= 0 ? "+" : ""}${Number(value).toFixed(2)}%`;
}

function formatNumber(value, digits = 2) {
  return value == null || Number.isNaN(Number(value)) ? "n/a" : Number(value).toFixed(digits);
}

function formatDate(value) {
  return value ? new Intl.DateTimeFormat("zh-TW", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(new Date(value)) : "n/a";
}

function formatPlayback(index, total) {
  return index && total ? `${index} / ${total}` : "n/a";
}

function getTickTime(tick) {
  return tick?.market_timestamp ?? tick?.timestamp ?? null;
}

function getTradeTime(trade) {
  return trade?.market_timestamp ?? trade?.timestamp ?? null;
}

function downloadFile(filename, content, mimeType) {
  const blob = new Blob([content], { type: mimeType });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(link.href);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function escapeAttribute(value) {
  return escapeHtml(value);
}

async function runAction(fn, options = {}) {
  try {
    await fn();
  } catch (error) {
    state.analysisStatus = { tone: "error", text: "操作失敗", detail: error.message || "Request failed" };
    renderAnalysisStatus();
    window.alert(error.message || "Request failed");
  } finally {
    if (typeof options.onFinally === "function") options.onFinally();
  }
}

function formatError(detail) {
  if (Array.isArray(detail)) return detail.map((item) => item.msg || JSON.stringify(item)).join("\n");
  return detail || "Request failed";
}

async function request(url, options = {}) {
  const response = await fetch(url, options);
  const payload = await response.json().catch(() => ({ detail: response.statusText }));
  if (!response.ok) throw new Error(formatError(payload.detail));
  return payload;
}
