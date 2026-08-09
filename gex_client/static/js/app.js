const sessionKey = "foxchase-gex-browser-session";
let sessionId = sessionStorage.getItem(sessionKey);
if (!sessionId) {
  sessionId = crypto.randomUUID();
  sessionStorage.setItem(sessionKey, sessionId);
}

const $ = id => document.getElementById(id);
let loadInFlight = false;

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
    renderPattern(data);
    renderChart(data, symbol);
    $("updated").textContent = `${data.display_symbol || symbol} updated ${data.updated_at}`;
    $("unit").textContent = data.unit || "shares per $ move";
    if (Number.isFinite(Number(data.online))) $("active-sessions").textContent = data.online;
  } catch (error) {
    showError(error.message);
    $("updated").textContent = "not connected";
  } finally {
    loadInFlight = false;
    $("refresh").disabled = false;
  }
}

async function heartbeat() {
  try {
    const response = await fetch("/api/presence", {
      method: "POST", headers: {"X-GEX-Session": sessionId}, cache: "no-store"
    });
    const data = await response.json();
    if (response.ok) $("active-sessions").textContent = data.online;
  } catch (_) {}
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

$("refresh").addEventListener("click", loadGex);
$("symbol").addEventListener("change", loadGex);
heartbeat();
if (isMarketRefreshWindow()) {
  loadGex();
} else {
  $("updated").textContent = "auto-refresh paused outside market hours";
}
setInterval(heartbeat, 30_000);
setInterval(() => { if (isMarketRefreshWindow()) loadGex(); }, 30_000);
