const CHART_SYMBOLS = ["NDX", "SPX"];
const SCALE_STORAGE_KEY = "foxchase-gex-scale";
const RAMP_CATEGORIES = Object.freeze([
  Object.freeze({key: "gamma-pin", index: 0, aliases: ["gamma pin", "pinned"]}),
  Object.freeze({key: "mixed-gamma", index: 1, aliases: ["mixed gamma"]}),
  Object.freeze({key: "forward-positive-ramp", index: 2, aliases: ["forward positive ramp"]}),
  Object.freeze({key: "backward-positive-ramp", index: 3, aliases: ["backward positive ramp", "backwards positive ramp"]}),
  Object.freeze({key: "forward-negative-ramp", index: 4, aliases: ["forward negative ramp"]}),
  Object.freeze({key: "backward-negative-ramp", index: 5, aliases: ["backward negative ramp", "backwards negative ramp"]})
]);
// The API's chart values are raw "shares per $ move", not millions.
// Keep presets in that same unit so a fixed range has no hidden conversion.
const SCALE_OPTIONS = Object.freeze({
  NDX: Object.freeze([25, 35, 75, 100, 200, 300, 400, 500, 1000]),
  SPX: Object.freeze([5, 10, 25, 50, 75, 100, 200, 300])
});
const GEX_BAR_GAP = 0.08;
const GEX_LABEL_SPACING_PX = 30;

const $ = id => document.getElementById(id);
let sessionId = "";

function readScale(symbol) {
  try {
    const stored = localStorage.getItem(`${SCALE_STORAGE_KEY}-${symbol}`);
    if (stored === "auto") return stored;
    if (SCALE_OPTIONS[symbol].some(value => String(value) === stored)) return stored;
  } catch (_) {
    // Storage can be unavailable in privacy-restricted browser contexts.
  }
  return "auto";
}

function saveScale(symbol, value) {
  try {
    localStorage.setItem(`${SCALE_STORAGE_KEY}-${symbol}`, value);
  } catch (_) {
    // The control still works for the current page when storage is unavailable.
  }
}

function finiteGexValues(data) {
  return (Array.isArray(data?.strikes) ? data.strikes : [])
    .map(row => Number(row.gex))
    .filter(Number.isFinite)
    .map(Math.abs);
}

function resolveScaleRange(data, state) {
  const selected = state?.scale ?? "auto";
  if (selected !== "auto") {
    const magnitude = Number(selected);
    if (Number.isFinite(magnitude) && magnitude > 0) return [-magnitude, magnitude];
  }

  const values = finiteGexValues(data);
  const maximum = Math.max(1, ...values);
  const target = maximum * 1.2;
  const prior = Number(state?.autoEdge);

  // Hold a stable auto range for modest refresh-to-refresh movement, but always
  // expand immediately when a new bar would otherwise lose its padding.
  const edge = Number.isFinite(prior) && target >= prior * 0.8 && target <= prior * 1.2
    ? prior
    : target;
  if (state) state.autoEdge = edge;
  return [-edge, edge];
}

function formatStrikeTick(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "";
  return number.toLocaleString("en-US", {
    maximumFractionDigits: Number.isInteger(number) ? 0 : 2
  });
}

function selectReadableTicks(strikes, chartHeight) {
  const values = (Array.isArray(strikes) ? strikes : [])
    .map(Number)
    .filter(Number.isFinite);
  if (!values.length) return {values: [], labels: [], step: 1};

  const measuredHeight = Number(chartHeight);
  const usableHeight = Number.isFinite(measuredHeight) && measuredHeight > 0
    ? Math.max(240, measuredHeight)
    : 620;
  const maxLabels = Math.max(2, Math.floor(usableHeight / GEX_LABEL_SPACING_PX));
  const step = Math.max(1, Math.ceil(values.length / maxLabels));
  const indices = [];

  for (let index = 0; index < values.length; index += step) indices.push(index);
  if (indices[indices.length - 1] !== values.length - 1) {
    indices.push(values.length - 1);
  }

  return {
    values: indices.map(index => values[index]),
    labels: indices.map(index => formatStrikeTick(values[index])),
    step
  };
}

function chartHeightPixels() {
  const chart = $("gexChart");
  const directHeight = Number(chart?.clientHeight);
  if (Number.isFinite(directHeight) && directHeight > 0) return directHeight;

  const measuredHeight = Number(chart?.getBoundingClientRect?.().height);
  if (Number.isFinite(measuredHeight) && measuredHeight > 0) return measuredHeight;

  const card = chart?.closest?.(".chart-card");
  const cardHeight = Number(card?.clientHeight);
  if (Number.isFinite(cardHeight) && cardHeight > 0) return Math.max(240, cardHeight - 138);

  return 620;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, character => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;"
  })[character]);
}

function showError(message = "") {
  const box = $("error");
  box.textContent = message;
  box.hidden = !message;
}

function showChartError(state, message = "") {
  const box = $(`chart-error-${state.symbol}`);
  box.textContent = message;
  box.hidden = !message;
}

function normalizePatternLabel(value) {
  return String(value ?? "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, " ")
    .trim()
    .replace(/\s+/g, " ");
}

function rampCategoryForValue(value) {
  const numeric = typeof value === "number"
    ? value
    : typeof value === "string" && value.trim() ? Number(value) : NaN;
  if (Number.isInteger(numeric) && numeric >= 0 && numeric < RAMP_CATEGORIES.length) {
    return RAMP_CATEGORIES[numeric].key;
  }
  const label = normalizePatternLabel(value);
  if (!label) return "";
  return RAMP_CATEGORIES.find(category =>
    category.aliases.some(alias => label.includes(normalizePatternLabel(alias)))
  )?.key || "";
}

function activeRampCategory(pattern) {
  if (!pattern || typeof pattern !== "object") return "";
  const explicit = [
    pattern.active_category,
    pattern.category,
    pattern.active_category_index,
    pattern.category_index,
    pattern.primary
  ];
  for (const candidate of explicit) {
    const category = rampCategoryForValue(candidate);
    if (category) return category;
  }
  return (Array.isArray(pattern.signals) ? pattern.signals : [])
    .map(signal => typeof signal === "string" ? signal : signal?.type)
    .map(rampCategoryForValue)
    .find(Boolean) || "";
}

function clearActiveRampCategory(symbol) {
  for (const item of document.querySelectorAll(".ramp-item[data-category]")) {
    item.classList.remove(`is-active-${symbol}`);
    const chip = item.querySelector(`.ramp-active-chip[data-symbol="${symbol}"]`);
    if (chip) chip.hidden = true;
  }
}

function renderActiveRampCategory(data, state) {
  clearActiveRampCategory(state.symbol);
  const category = activeRampCategory(data?.patterns);
  if (!category) return;
  const item = [...document.querySelectorAll(".ramp-item[data-category]")]
    .find(candidate => candidate.dataset.category === category);
  if (!item) return;
  item.classList.add(`is-active-${state.symbol}`);
  const chip = item.querySelector(`.ramp-active-chip[data-symbol="${state.symbol}"]`);
  if (chip) chip.hidden = false;
}

function renderChart(data, state) {
  const rows = (Array.isArray(data.strikes) ? data.strikes : [])
    .filter(row => Number.isFinite(Number(row.strike)) && Number.isFinite(Number(row.gex)))
    .sort((a, b) => Number(a.strike) - Number(b.strike));
  const strikes = rows.map(row => Number(row.strike));
  const positive = rows.map(row => Number(row.gex) > 0 ? Number(row.gex) : 0);
  const negative = rows.map(row => Number(row.gex) < 0 ? Number(row.gex) : 0);
  const range = resolveScaleRange({strikes: rows}, state);
  const tickSelection = selectReadableTicks(strikes, chartHeightPixels());
  const spot = Number(data.spot);

  const traces = [
    {
      type: "bar", orientation: "h", y: strikes, x: negative,
      marker: {color: "rgb(225, 0, 0)", line: {color: "rgba(255,255,255,.08)", width: .5}},
      hovertemplate: "Strike %{y}<br>GEX %{x:.2f}<extra></extra>"
    },
    {
      type: "bar", orientation: "h", y: strikes, x: positive,
      marker: {color: "rgb(0, 155, 90)", line: {color: "rgba(255,255,255,.08)", width: .5}},
      hovertemplate: "Strike %{y}<br>GEX %{x:.2f}<extra></extra>"
    }
  ];
  const shapes = [];
  const annotations = [];
  if (Number.isFinite(spot)) {
    shapes.push({
      type: "line", xref: "x", yref: "y",
      x0: range[0], x1: range[1], y0: spot, y1: spot,
      line: {color: "#d8bf00", width: 1, dash: "dot"}
    });
    annotations.push({
      xref: "x", yref: "y", x: range[1], y: spot,
      text: spot.toFixed(2), showarrow: false,
      xanchor: "right", yanchor: "bottom", font: {color: "#d8bf00", size: 12}
    });
  }
  const layout = {
    paper_bgcolor: "#2b2b2b", plot_bgcolor: "#2b2b2b",
    font: {color: "#e8e8e8", size: 12},
    margin: {l: 72, r: 94, t: 18, b: 34},
    barmode: "overlay", bargap: GEX_BAR_GAP, showlegend: false,
    xaxis: {
      range, autorange: false, zeroline: true, zerolinecolor: "#777", zerolinewidth: 1,
      gridcolor: "#3d3d3d", tickfont: {color: "#f0f0f0", size: 10}
    },
    yaxis: {
      tickmode: "array", tickvals: tickSelection.values, ticktext: tickSelection.labels,
      separatethousands: false,
      gridcolor: "#303030", tickfont: {color: "#f0f0f0", size: 10}
    },
    shapes,
    annotations
  };
  Plotly.react(`gexChart-${state.symbol}`, traces, layout, {
    displayModeBar: false, responsive: true
  });
}

function formatHistoricalTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York", hour: "numeric", minute: "2-digit", second: "2-digit"
  }).format(date);
}

function formatAge(value) {
  const stamp = new Date(value);
  if (Number.isNaN(stamp.getTime())) return "age unavailable";
  const seconds = Math.max(0, Math.round((Date.now() - stamp.getTime()) / 1000));
  if (seconds < 60) return `${seconds}s old`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m old`;
  return `${Math.floor(minutes / 60)}h old`;
}

function setGlobalStatus(text) {
  $("updated").textContent = text;
}

function chartSessionId(symbol) {
  return `${sessionId}-${symbol.toLowerCase()}`;
}

function renderResult(data, state, historical = false) {
  state.data = data;
  state.historical = historical;
  renderActiveRampCategory(data, state);
  renderChart(data, state);
  const stamp = data.captured_at || data.updated_at;
  const time = formatHistoricalTime(stamp);
  if (historical) {
    $(`chart-status-${state.symbol}`).textContent =
      `${state.symbol} historical · ${data.historical_date} · ${time || "time unavailable"} ET`;
    $(`chart-mode-${state.symbol}`).textContent = "historical · local archive";
  } else {
    $(`chart-status-${state.symbol}`).textContent =
      `${state.symbol} updated ${time || "time unavailable"} ET · ${formatAge(stamp)}`;
    $(`chart-mode-${state.symbol}`).textContent = "live · local";
  }
  $(`expiration-${state.symbol}`).textContent =
    `expiration · ${data.expiration_date || "—"}`;
  $(`unit-${state.symbol}`).textContent = data.unit || "shares per $ move";
  showChartError(state);
}

function updateGlobalStatus() {
  const historical = CHART_SYMBOLS.filter(symbol => chartStates[symbol].historical);
  setGlobalStatus(historical.length
    ? `${historical.length} chart${historical.length === 1 ? "" : "s"} showing local history`
    : "live · NDX + SPX · local Schwab data");
}

async function loadGex(state) {
  if (state.loadInFlight) return;
  state.loadInFlight = true;
  $(`chart-status-${state.symbol}`).textContent =
    `loading ${state.symbol} from local Schwab connection…`;
  showChartError(state);
  try {
    const response = await fetch(`/api/gex/${state.symbol}`, {
      headers: {"X-GEX-Session": chartSessionId(state.symbol)}, cache: "no-store"
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "GEX request failed");
    renderResult(data, state, false);
  } catch (error) {
    clearActiveRampCategory(state.symbol);
    showChartError(state, error.message);
    $(`chart-status-${state.symbol}`).textContent = "not connected";
  } finally {
    state.loadInFlight = false;
  }
}

async function loadHistorySessions(state) {
  const picker = $(`history-session-${state.symbol}`);
  const selected = picker.value;
  try {
    const response = await fetch(`/api/history/${state.symbol}/sessions`, {cache: "no-store"});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "history lookup failed");
    const sessions = data.sessions || [];
    picker.innerHTML = `<option value="">live</option>` + sessions.map(session =>
      `<option value="${escapeHtml(session.date)}">${escapeHtml(session.date)} · ${session.captures} snapshots</option>`
    ).join("") + (sessions.length ? "" : `<option value="" disabled>no archived sessions yet</option>`);
    picker.value = sessions.some(session => session.date === selected) ? selected : "";
    if (!picker.value) {
      state.historyTimeline = [];
      $(`history-time-field-${state.symbol}`).hidden = true;
    }
  } catch (_) {
    picker.innerHTML = `<option value="">live</option><option value="" disabled>archive unavailable</option>`;
    state.historyTimeline = [];
    $(`history-time-field-${state.symbol}`).hidden = true;
  }
}

async function loadHistoryTimeline(state) {
  const day = $(`history-session-${state.symbol}`).value;
  if (!day) {
    state.historyTimeline = [];
    state.historical = false;
    $(`history-time-field-${state.symbol}`).hidden = true;
    await loadGex(state);
    updateGlobalStatus();
    return;
  }
  $(`chart-status-${state.symbol}`).textContent =
    `loading ${state.symbol} archive for ${day}…`;
  showChartError(state);
  try {
    const response = await fetch(`/api/history/${state.symbol}/${day}/timeline`, {cache: "no-store"});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "historical session failed");
    state.historyTimeline = data.timeline || [];
    const slider = $(`history-time-${state.symbol}`);
    slider.min = "0";
    slider.max = String(Math.max(0, state.historyTimeline.length - 1));
    slider.value = slider.max;
    $(`history-time-field-${state.symbol}`).hidden = state.historyTimeline.length === 0;
    await loadHistoricalSnapshot(state);
  } catch (error) {
    clearActiveRampCategory(state.symbol);
    showChartError(state, error.message);
    $(`chart-status-${state.symbol}`).textContent = "historical archive unavailable";
  }
  updateGlobalStatus();
}

async function loadHistoricalSnapshot(state) {
  const day = $(`history-session-${state.symbol}`).value;
  const index = Number($(`history-time-${state.symbol}`).value || 0);
  const point = state.historyTimeline[index];
  $(`history-time-label-${state.symbol}`).textContent =
    point ? formatHistoricalTime(point.captured_at) : "";
  if (!day || !point) return;
  try {
    const response = await fetch(
      `/api/history/${state.symbol}/${day}/snapshot?index=${encodeURIComponent(index)}`,
      {cache: "no-store"}
    );
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "historical snapshot failed");
    renderResult(data, state, true);
  } catch (error) {
    clearActiveRampCategory(state.symbol);
    showChartError(state, error.message);
  }
}

async function refreshAll() {
  const button = $("refresh");
  button.disabled = true;
  showError();
  try {
    await Promise.all(CHART_SYMBOLS.map(symbol => {
      const state = chartStates[symbol];
      return $(`history-session-${symbol}`).value
        ? loadHistoricalSnapshot(state)
        : loadGex(state);
    }));
  } finally {
    button.disabled = false;
    updateGlobalStatus();
  }
}

async function heartbeat() {
  try {
    const response = await fetch("/api/presence", {
      method: "POST", headers: {"X-GEX-Session": sessionId}, cache: "no-store"
    });
    const data = await response.json();
    const online = Number(data.online);
    if (!response.ok || !Number.isFinite(online)) throw new Error("Presence unavailable");
    $("active-sessions").textContent = String(Math.max(0, Math.trunc(online)));
  } catch (_) {
    $("active-sessions").textContent = "—";
  }
}

async function checkSetup() {
  try {
    const response = await fetch("/api/setup-status", {cache: "no-store"});
    const setup = await response.json();
    const card = $("setup-card");
    card.hidden = setup.ready;
    if (!setup.ready) {
      const state = [
        `App key: ${setup.client_id_configured ? "configured" : "missing"}`,
        `App secret: ${setup.client_secret_configured ? "configured" : "missing"}`,
        `OAuth token: ${setup.token_configured ? "configured" : "missing"}`
      ];
      $("setup-state").textContent = state.join("  •  ");
      setGlobalStatus("Schwab connection setup required");
    }
    return setup.ready;
  } catch (_) {
    return false;
  }
}

function isMarketRefreshWindow() {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York", hour12: false,
    weekday: "short", hour: "2-digit", minute: "2-digit"
  }).formatToParts(new Date());
  const values = Object.fromEntries(parts.map(part => [part.type, part.value]));
  const minutes = Number(values.hour) * 60 + Number(values.minute);
  return !["Sat", "Sun"].includes(values.weekday) && minutes >= 565 && minutes <= 970;
}

const chartStates = Object.fromEntries(CHART_SYMBOLS.map(symbol => [symbol, {
  symbol,
  data: null,
  historical: false,
  historyTimeline: [],
  historyLoadTimer: null,
  loadInFlight: false,
  scale: readScale(symbol),
  autoEdge: null
}]));

function boot() {
  const sessionKey = "foxchase-gex-browser-session";
  sessionId = sessionStorage.getItem(sessionKey);
  if (!sessionId) {
    sessionId = crypto.randomUUID();
    sessionStorage.setItem(sessionKey, sessionId);
  }

  $("refresh").addEventListener("click", refreshAll);
  for (const symbol of CHART_SYMBOLS) {
    const state = chartStates[symbol];
    const scale = $(`scale-${symbol}`);
    scale.value = state.scale;
    scale.addEventListener("change", () => {
      state.scale = scale.value;
      saveScale(symbol, state.scale);
      if (state.data) renderChart(state.data, state);
    });

    $(`history-session-${symbol}`).addEventListener("change", () => loadHistoryTimeline(state));
    $(`history-time-${symbol}`).addEventListener("input", () => {
      const index = Number($(`history-time-${symbol}`).value || 0);
      const point = state.historyTimeline[index];
      $(`history-time-label-${symbol}`).textContent = point ? formatHistoricalTime(point.captured_at) : "";
      clearTimeout(state.historyLoadTimer);
      state.historyLoadTimer = setTimeout(() => loadHistoricalSnapshot(state), 100);
    });
  }

  heartbeat();
  Promise.all(CHART_SYMBOLS.map(symbol => loadHistorySessions(chartStates[symbol]))).then(async () => {
    const ready = await checkSetup();
    if (!ready) return;
    if (isMarketRefreshWindow()) {
      await Promise.all(CHART_SYMBOLS.map(symbol => loadGex(chartStates[symbol])));
      updateGlobalStatus();
    } else {
      setGlobalStatus("connected · auto-refresh paused outside market hours");
    }
  });
  setInterval(heartbeat, 30_000);
  setInterval(() => {
    if (!isMarketRefreshWindow()) return;
    const liveStates = CHART_SYMBOLS
      .map(symbol => chartStates[symbol])
      .filter(state => !$(`history-session-${state.symbol}`).value);
    Promise.all(liveStates.map(loadGex)).then(updateGlobalStatus);
  }, 30_000);
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    CHART_SYMBOLS, SCALE_OPTIONS, RAMP_CATEGORIES, resolveScaleRange,
    activeRampCategory, rampCategoryForValue, selectReadableTicks
  };
}

if (typeof document !== "undefined") boot();
