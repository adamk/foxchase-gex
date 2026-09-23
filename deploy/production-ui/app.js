const CHART_SYMBOLS = Object.freeze(["NDX", "SPX"]);
const SCALE_STORAGE_KEY = "foxchase-gex-scale";

// Production GEX values are plotted in the API's existing raw unit (not millions).
// These ranges are view-only controls; they do not alter the /api/gex contract.
const SCALE_OPTIONS = Object.freeze({
  NDX: Object.freeze([25, 35, 45, 75, 100, 200, 300, 400, 500]),
  SPX: Object.freeze([10, 20, 30, 50, 75, 100, 200, 300])
});

const GEX_BAR_GAP = 0.08;
const GEX_LABEL_SPACING_PX = 30;
const NDX_DOMAIN_GEX_COVERAGE = 0.99;
const NDX_MATERIAL_GEX_FRACTION = 0.01;
const CLASSIFICATION_TONE_CLASSES = Object.freeze({
  "gamma pin": "chart-classification--gamma-pin",
  "mixed gamma": "chart-classification--mixed",
  "forward positive ramp": "chart-classification--positive",
  "backward positive ramp": "chart-classification--positive",
  "forward negative ramp": "chart-classification--negative",
  "backward negative ramp": "chart-classification--negative"
});
const CLASSIFICATION_CLASSES = Object.freeze([
  "chart-classification--neutral",
  ...new Set(Object.values(CLASSIFICATION_TONE_CLASSES))
]);

const $ = id => document.getElementById(id);

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

  const maximum = Math.max(1, ...finiteGexValues(data));
  const target = maximum * 1.2;
  const prior = Number(state?.autoEdge);

  // Keep modest refresh-to-refresh movement stable, while expanding immediately
  // when a new bar would otherwise lose its padding.
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

function chartHeightPixels(symbol) {
  const chart = $(`gexChart-${symbol}`);
  const directHeight = Number(chart?.clientHeight);
  if (Number.isFinite(directHeight) && directHeight > 0) return directHeight;

  const measuredHeight = Number(chart?.getBoundingClientRect?.().height);
  if (Number.isFinite(measuredHeight) && measuredHeight > 0) return measuredHeight;

  const card = chart?.closest?.(".chart-card");
  const cardHeight = Number(card?.clientHeight);
  if (Number.isFinite(cardHeight) && cardHeight > 0) return Math.max(240, cardHeight - 138);

  return 620;
}

function resolveStrikeDomain(data, state) {
  // Preserve the existing SPX autorange. NDX is the only production chart
  // with a wide, sparse strike tail that can dominate the viewport.
  if (state?.symbol !== "NDX") return null;

  const rows = (Array.isArray(data?.strikes) ? data.strikes : [])
    .map(row => ({
      strike: Number(row?.strike),
      magnitude: Math.abs(Number(row?.gex))
    }))
    .filter(row => Number.isFinite(row.strike) && Number.isFinite(row.magnitude))
    .sort((a, b) => a.strike - b.strike);
  const spot = Number(data?.spot);
  if (!rows.length || !Number.isFinite(spot)) return null;

  const anchor = rows.reduce((best, row, index) =>
    Math.abs(row.strike - spot) < Math.abs(rows[best].strike - spot) ? index : best, 0);
  const totalMagnitude = rows.reduce((sum, row) => sum + row.magnitude, 0);
  const targetMagnitude = totalMagnitude * NDX_DOMAIN_GEX_COVERAGE;
  const maximumMagnitude = Math.max(...rows.map(row => row.magnitude));
  const materialMagnitude = maximumMagnitude * NDX_MATERIAL_GEX_FRACTION;

  let lowerIndex = 0;
  let upperIndex = rows.length - 1;
  if (targetMagnitude > 0) {
    let best = null;
    for (let left = 0; left <= anchor; left += 1) {
      let mass = 0;
      for (let right = left; right < rows.length; right += 1) {
        mass += rows[right].magnitude;
        if (right >= anchor && mass >= targetMagnitude) {
          const candidate = {
            span: rows[right].strike - rows[left].strike,
            left,
            right,
            mass
          };
          if (!best || candidate.span < best.span ||
              (candidate.span === best.span && candidate.mass > best.mass)) {
            best = candidate;
          }
          break;
        }
      }
    }
    if (best) {
      lowerIndex = best.left;
      upperIndex = best.right;
    }
  }

  // Keep any individually material remote concentration visible even when the
  // remaining tail is too small to affect the cumulative-mass interval.
  const materialIndices = rows
    .map((row, index) => row.magnitude >= materialMagnitude ? index : -1)
    .filter(index => index >= 0);
  if (materialIndices.length) {
    lowerIndex = Math.min(lowerIndex, materialIndices[0]);
    upperIndex = Math.max(upperIndex, materialIndices[materialIndices.length - 1]);
  }

  const activeLower = Math.min(rows[lowerIndex].strike, spot);
  const activeUpper = Math.max(rows[upperIndex].strike, spot);
  const gaps = rows.slice(lowerIndex, upperIndex + 1)
    .map((row, index, activeRows) => index ? row.strike - activeRows[index - 1].strike : 0)
    .filter(gap => gap > 0);
  const sortedGaps = [...gaps].sort((a, b) => a - b);
  const typicalGap = sortedGaps.length
    ? sortedGaps[Math.floor(sortedGaps.length / 2)]
    : Math.max(1, activeUpper - activeLower);
  const activeSpan = Math.max(activeUpper - activeLower, typicalGap * 4, 1);
  const padding = Math.max(activeSpan * 0.08, typicalGap * 2);
  return [activeLower - padding, activeUpper + padding];
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

function clearClassification(symbol) {
  const badge = $(`classification-${symbol}`);
  if (!badge) return;
  badge.textContent = "";
  badge.hidden = true;
  badge.classList.remove(...CLASSIFICATION_CLASSES);
  badge.classList.add("chart-classification--neutral");
}

function classificationToneClass(primary) {
  const key = typeof primary === "string"
    ? primary.trim().toLowerCase()
    : "";
  return CLASSIFICATION_TONE_CLASSES[key] || "chart-classification--neutral";
}

function renderClassification(data, state) {
  clearClassification(state.symbol);
  const badge = $(`classification-${state.symbol}`);
  const primary = typeof data?.patterns?.primary === "string"
    ? data.patterns.primary.trim()
    : "";
  if (!badge || !primary) return;
  badge.textContent = primary;
  badge.classList.remove("chart-classification--neutral");
  badge.classList.add(classificationToneClass(primary));
  badge.hidden = false;
}

function renderChart(data, state) {
  const rows = (Array.isArray(data.strikes) ? data.strikes : [])
    .filter(row => Number.isFinite(Number(row.strike)) && Number.isFinite(Number(row.gex)))
    .sort((a, b) => Number(a.strike) - Number(b.strike));
  const strikes = rows.map(row => Number(row.strike));
  const positive = rows.map(row => Number(row.gex) > 0 ? Number(row.gex) : 0);
  const negative = rows.map(row => Number(row.gex) < 0 ? Number(row.gex) : 0);
  const range = resolveScaleRange({strikes: rows}, state);
  const strikeDomain = resolveStrikeDomain({strikes: rows, spot: data.spot}, state);
  const tickSelection = selectReadableTicks(strikes, chartHeightPixels(state.symbol));
  const spot = Number(data.spot);

  const traces = [
    {
      type: "bar",
      orientation: "h",
      y: strikes,
      x: negative,
      marker: {
        color: "rgb(225, 0, 0)",
        line: {color: "rgba(255,255,255,.08)", width: .5}
      },
      hovertemplate: "Strike %{y}<br>GEX %{x:.2f}<extra></extra>"
    },
    {
      type: "bar",
      orientation: "h",
      y: strikes,
      x: positive,
      marker: {
        color: "rgb(0, 155, 90)",
        line: {color: "rgba(255,255,255,.08)", width: .5}
      },
      hovertemplate: "Strike %{y}<br>GEX %{x:.2f}<extra></extra>"
    }
  ];

  const shapes = [];
  const annotations = [];
  if (Number.isFinite(spot)) {
    shapes.push({
      type: "line",
      xref: "x",
      yref: "y",
      x0: range[0],
      x1: range[1],
      y0: spot,
      y1: spot,
      line: {color: "#d8bf00", width: 1, dash: "dot"}
    });
    annotations.push({
      xref: "x",
      yref: "y",
      x: range[1],
      y: spot,
      text: spot.toFixed(2),
      showarrow: false,
      xanchor: "right",
      yanchor: "bottom",
      font: {color: "#d8bf00", size: 12}
    });
  }

  const layout = {
    paper_bgcolor: "#2b2b2b",
    plot_bgcolor: "#2b2b2b",
    font: {color: "#e8e8e8", size: 12},
    margin: {l: 72, r: 94, t: 18, b: 34},
    barmode: "overlay",
    bargap: GEX_BAR_GAP,
    showlegend: false,
    xaxis: {
      range,
      autorange: false,
      zeroline: true,
      zerolinecolor: "#777",
      zerolinewidth: 1,
      gridcolor: "#3d3d3d",
      tickfont: {color: "#f0f0f0", size: 10}
    },
    yaxis: {
      tickmode: "array",
      tickvals: tickSelection.values,
      ticktext: tickSelection.labels,
      ...(strikeDomain ? {range: strikeDomain, autorange: false} : {}),
      separatethousands: false,
      gridcolor: "#303030",
      tickfont: {color: "#f0f0f0", size: 10}
    },
    shapes,
    annotations
  };

  if (!window.Plotly) throw new Error("chart renderer unavailable");
  Plotly.react(`gexChart-${state.symbol}`, traces, layout, {
    displayModeBar: false,
    responsive: true
  });
}

function formatTimestamp(value) {
  if (!value) return "time unavailable";
  const stamp = new Date(value);
  if (Number.isNaN(stamp.getTime())) return String(value);
  return new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    hour: "numeric",
    minute: "2-digit",
    second: "2-digit"
  }).format(stamp);
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

function renderResult(data, state) {
  state.data = data;
  renderClassification(data, state);
  renderChart(data, state);

  const displaySymbol = data.display_symbol || state.symbol;
  const stamp = data.updated_at;
  $(`chart-status-${state.symbol}`).textContent =
    `${displaySymbol} updated ${formatTimestamp(stamp)} ET · ${formatAge(stamp)}`;
  $(`chart-mode-${state.symbol}`).textContent = "live";
  $(`expiration-${state.symbol}`).textContent =
    `expiration · ${data.expiration_date || data.expiration || "—"}`;
  $(`source-${state.symbol}`).textContent = `source · ${data.source || "production"}`;
  $(`unit-${state.symbol}`).textContent = data.unit || "shares per $ move";
  $("updated").textContent = "live · NDX + SPX · production market data";
  showChartError(state);
}

async function loadGex(state) {
  if (state.loadInFlight) return;
  state.loadInFlight = true;
  $(`chart-status-${state.symbol}`).textContent = `loading ${state.symbol}…`;
  showChartError(state);

  try {
    const response = await fetch(`/api/gex/${state.symbol}`, {
      cache: "no-store"
    });
    const data = await response.json();
    if (!response.ok || data.error) {
      throw new Error(data.error || "GEX request failed");
    }
    renderResult(data, state);
  } catch (error) {
    clearClassification(state.symbol);
    showChartError(state, error.message || "GEX request failed");
    $(`chart-status-${state.symbol}`).textContent = "not connected";
    showError(`${state.symbol}: ${error.message || "GEX request failed"}`);
  } finally {
    state.loadInFlight = false;
  }
}

async function refreshAll() {
  const button = $("refresh");
  button.disabled = true;
  showError();
  try {
    await Promise.all(CHART_SYMBOLS.map(symbol => loadGex(chartStates[symbol])));
  } finally {
    button.disabled = false;
  }
}

function isMarketRefreshWindow() {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    hour12: false,
    weekday: "short",
    hour: "2-digit",
    minute: "2-digit"
  }).formatToParts(new Date());
  const values = Object.fromEntries(parts.map(part => [part.type, part.value]));
  const minutes = Number(values.hour) * 60 + Number(values.minute);
  return !["Sat", "Sun"].includes(values.weekday) && minutes >= 565 && minutes <= 970;
}

const chartStates = Object.fromEntries(CHART_SYMBOLS.map(symbol => [symbol, {
  symbol,
  data: null,
  loadInFlight: false,
  scale: readScale(symbol),
  autoEdge: null
}]));

function boot() {
  $("refresh").addEventListener("click", refreshAll);

  for (const symbol of CHART_SYMBOLS) {
    const state = chartStates[symbol];
    const scale = $(`scale-${symbol}`);
    scale.value = state.scale;
    scale.addEventListener("change", () => {
      state.scale = scale.value;
      saveScale(symbol, state.scale);
      // A view-only scale change must never trigger an API request.
      if (state.data) renderChart(state.data, state);
    });
  }

  refreshAll();
  setInterval(() => {
    if (isMarketRefreshWindow()) refreshAll();
  }, 5_000);
}

if (typeof document !== "undefined") boot();

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    CHART_SYMBOLS,
    SCALE_OPTIONS,
    resolveScaleRange,
    resolveStrikeDomain,
    selectReadableTicks,
    readScale,
    clearClassification,
    classificationToneClass,
    renderClassification,
    loadGex
  };
}
