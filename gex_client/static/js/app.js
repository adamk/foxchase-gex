const sessionKey = "foxchase-gex-browser-session";
let sessionId = sessionStorage.getItem(sessionKey);
if (!sessionId) {
  sessionId = crypto.randomUUID();
  sessionStorage.setItem(sessionKey, sessionId);
}

const $ = id => document.getElementById(id);
let loadInFlight = false;
let historyTimeline = [];
let historyLoadTimer = null;

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

function renderPattern(data) {
  const box = $("pattern-box");
  const pattern = data.patterns;
  if (!pattern) {
    box.hidden = true;
    return;
  }
  const signals = (pattern.signals || []).slice(0, 3).map(signal =>
    `<span>• ${escapeHtml(signal.type)}</span>`
  ).join("");
  box.innerHTML = `
    <div class="pattern-kicker">Foxchase Read</div>
    <div class="pattern-primary">${escapeHtml(pattern.read_title || pattern.primary)}</div>
    <div class="pattern-summary">${escapeHtml(pattern.action_read || pattern.summary)}</div>
    <div class="pattern-key">${escapeHtml(pattern.key_read || "")}</div>
    <div class="pattern-signals">${signals}</div>`;
  box.hidden = false;
}

function renderChart(data, symbol) {
  const rows = [...data.strikes].sort((a, b) => a.strike - b.strike);
  const strikes = rows.map(row => row.strike);
  const positive = rows.map(row => row.gex > 0 ? row.gex : 0);
  const negative = rows.map(row => row.gex < 0 ? row.gex : 0);
  const maximum = Math.max(1, ...rows.map(row => Math.abs(row.gex)));
  const edge = maximum * 1.2;
  const range = [-edge, edge];

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
  const layout = {
    paper_bgcolor: "#2b2b2b", plot_bgcolor: "#2b2b2b",
    font: {color: "#e8e8e8", size: 10},
    margin: {l: 62, r: 96, t: 22, b: 28},
    barmode: "overlay", bargap: .28, showlegend: false,
    xaxis: {
      range, zeroline: true, zerolinecolor: "#777", zerolinewidth: 1,
      gridcolor: "#3d3d3d", tickfont: {color: "#f0f0f0", size: 8}
    },
    yaxis: {
      tickmode: "linear", dtick: symbol === "NDX" ? 10 : 5,
      tickformat: ".0f", separatethousands: false,
      gridcolor: "#303030", tickfont: {color: "#f0f0f0", size: 8}
    },
    shapes: [{
      type: "line", xref: "x", yref: "y",
      x0: range[0], x1: range[1], y0: data.spot, y1: data.spot,
      line: {color: "#d8bf00", width: 1, dash: "dot"}
    }],
    annotations: [{
      xref: "x", yref: "y", x: range[1], y: data.spot,
      text: Number(data.spot).toFixed(2), showarrow: false,
      xanchor: "right", yanchor: "bottom", font: {color: "#d8bf00", size: 10}
    }]
  };
  Plotly.react("gexChart", traces, layout, {displayModeBar: false, responsive: true});
}

function formatHistoricalTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York", hour: "numeric", minute: "2-digit", second: "2-digit"
  }).format(date);
}

function renderResult(data, symbol, historical = false) {
  renderPattern(data);
  renderChart(data, symbol);
  if (historical) {
    const stamp = data.captured_at || data.updated_at;
    $("updated").textContent = `${symbol} historical · ${data.historical_date} · ${formatHistoricalTime(stamp)} ET`;
    $("chart-mode").textContent = "historical · local archive";
  } else {
    $("updated").textContent = `${data.display_symbol || symbol} updated ${data.updated_at}`;
    $("chart-mode").textContent = "live · local";
  }
  $("unit").textContent = data.unit || "shares per $ move";
  if (!historical && Number.isFinite(Number(data.online))) {
    $("active-sessions").textContent = data.online;
  }
}

async function loadGex() {
  if (loadInFlight) return;
  loadInFlight = true;
  const symbol = $("symbol").value;
  $("refresh").disabled = true;
  $("updated").textContent = `loading ${symbol} from local Schwab connection…`;
  showError();
  try {
    const response = await fetch(`/api/gex/${symbol}`, {
      headers: {"X-GEX-Session": sessionId}, cache: "no-store"
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "GEX request failed");
    renderResult(data, symbol, false);
  } catch (error) {
    showError(error.message);
    $("updated").textContent = "not connected";
  } finally {
    loadInFlight = false;
    $("refresh").disabled = false;
  }
}

async function loadHistorySessions() {
  const symbol = $("symbol").value;
  const picker = $("history-session");
  const selected = picker.value;
  try {
    const response = await fetch(`/api/history/${symbol}/sessions`, {cache: "no-store"});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "history lookup failed");
    const sessions = data.sessions || [];
    picker.innerHTML = `<option value="">live</option>` + sessions.map(session =>
      `<option value="${escapeHtml(session.date)}">${escapeHtml(session.date)} · ${session.captures} snapshots</option>`
    ).join("") + (sessions.length ? "" : `<option value="" disabled>no archived sessions yet</option>`);
    picker.value = sessions.some(session => session.date === selected) ? selected : "";
    if (!picker.value) {
      $("history-time-field").hidden = true;
      historyTimeline = [];
    }
  } catch (_) {
    picker.innerHTML = `<option value="">live</option><option value="" disabled>archive unavailable</option>`;
    $("history-time-field").hidden = true;
  }
}

async function loadHistoryTimeline() {
  const symbol = $("symbol").value;
  const day = $("history-session").value;
  if (!day) {
    historyTimeline = [];
    $("history-time-field").hidden = true;
    loadGex();
    return;
  }
  showError();
  $("updated").textContent = `loading ${symbol} archive for ${day}…`;
  try {
    const response = await fetch(`/api/history/${symbol}/${day}/timeline`, {cache: "no-store"});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "historical session failed");
    historyTimeline = data.timeline || [];
    const slider = $("history-time");
    slider.min = "0";
    slider.max = String(Math.max(0, historyTimeline.length - 1));
    slider.value = slider.max;
    $("history-time-field").hidden = historyTimeline.length === 0;
    await loadHistoricalSnapshot();
  } catch (error) {
    showError(error.message);
    $("updated").textContent = "historical archive unavailable";
  }
}

async function loadHistoricalSnapshot() {
  const symbol = $("symbol").value;
  const day = $("history-session").value;
  const index = Number($("history-time").value || 0);
  const point = historyTimeline[index];
  $("history-time-label").textContent = point ? formatHistoricalTime(point.captured_at) : "";
  if (!day || !point) return;
  try {
    const response = await fetch(
      `/api/history/${symbol}/${day}/snapshot?index=${encodeURIComponent(index)}`,
      {cache: "no-store"}
    );
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "historical snapshot failed");
    renderResult(data, symbol, true);
    showError();
  } catch (error) {
    showError(error.message);
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
      $("updated").textContent = "Schwab connection setup required";
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

$("refresh").addEventListener("click", () => {
  if ($("history-session").value) loadHistoricalSnapshot();
  else loadGex();
});
$("symbol").addEventListener("change", async () => {
  $("history-session").value = "";
  await loadHistorySessions();
  loadGex();
});
$("history-session").addEventListener("change", loadHistoryTimeline);
$("history-time").addEventListener("input", () => {
  const index = Number($("history-time").value || 0);
  const point = historyTimeline[index];
  $("history-time-label").textContent = point ? formatHistoricalTime(point.captured_at) : "";
  clearTimeout(historyLoadTimer);
  historyLoadTimer = setTimeout(loadHistoricalSnapshot, 100);
});
heartbeat();
loadHistorySessions();
checkSetup().then(ready => {
  if (!ready) return;
  if (isMarketRefreshWindow()) loadGex();
  else $("updated").textContent = "connected · auto-refresh paused outside market hours";
});
setInterval(heartbeat, 30_000);
setInterval(() => {
  if (isMarketRefreshWindow() && !$("history-session").value) loadGex();
}, 30_000);
