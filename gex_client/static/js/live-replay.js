/* Current-session replay of the local collector archive; historical controls stay untouched. */
(() => {
  const SYMBOLS = ["NDX", "SPX"];
  const STALE_MS = 90_000;
  const ET = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York", hour: "numeric", minute: "2-digit", second: "2-digit", timeZoneName: "short"
  });

  class ReplayState {
    constructor() {
      this.points = [];
      this.mode = "live";
      this.selectedStamp = null;
      this.cursor = 0;
      this.sessionDate = null;
    }

    reset(sessionDate) {
      this.points = [];
      this.mode = "live";
      this.selectedStamp = null;
      this.cursor = 0;
      this.sessionDate = sessionDate;
    }

    merge(response) {
      const changedSession = this.sessionDate !== null && this.sessionDate !== response.session_date;
      if (this.sessionDate === null || changedSession) this.reset(response.session_date);
      const byStamp = new Map(this.points.map(point => [point.captured_at, point]));
      for (const point of response.points || []) {
        const value = Date.parse(point.captured_at);
        if (!Number.isFinite(value)) throw new Error("Invalid recorded timestamp");
        byStamp.set(point.captured_at, { ...point, time: value });
        this.cursor = Math.max(this.cursor, Number(point.cursor) || 0);
      }
      this.points = [...byStamp.values()].sort((a, b) => a.time - b.time || a.cursor - b.cursor);
      if (changedSession || this.mode === "live") this.goLive();
      else if (!this.points.some(point => point.captured_at === this.selectedStamp)) this.goLive();
      return { changedSession, count: this.points.length };
    }

    selected() {
      return this.points.find(point => point.captured_at === this.selectedStamp) || null;
    }

    select(index) {
      const point = this.points[index];
      if (!point) return null;
      if (index === this.points.length - 1) return this.goLive();
      this.mode = "replay";
      this.selectedStamp = point.captured_at;
      return point;
    }

    goLive() {
      this.mode = "live";
      this.selectedStamp = this.points.length ? this.points[this.points.length - 1].captured_at : null;
      return this.selected();
    }
  }

  if (typeof module !== "undefined" && module.exports) module.exports = { ReplayState, formatET };

  function formatET(value) {
    const stamp = new Date(value);
    return Number.isNaN(stamp.getTime()) ? "Time unavailable" : ET.format(stamp);
  }
  if (typeof document === "undefined") return;

  const host = window.GEXReplayHost;
  const inHistory = symbol => Boolean(document.getElementById(`history-session-${symbol}`).value);

  const states = Object.fromEntries(SYMBOLS.map(symbol => [symbol, new ReplayState()]));
  const rendered = Object.fromEntries(SYMBOLS.map(symbol => [symbol, null]));
  const requestEpoch = Object.fromEntries(SYMBOLS.map(symbol => [symbol, 0]));
  const pollEpoch = Object.fromEntries(SYMBOLS.map(symbol => [symbol, 0]));
  const drawTimers = Object.fromEntries(SYMBOLS.map(symbol => [symbol, null]));
  const snapshotCache = Object.fromEntries(SYMBOLS.map(symbol => [symbol, new Map()]));
  const byId = id => document.getElementById(id);
  function updateControls(symbol) {
    const state = states[symbol];
    const slider = byId(`live-replay-time-${symbol}`);
    const label = byId(`live-replay-label-${symbol}`);
    const live = byId(`live-replay-live-${symbol}`);
    const status = byId(`live-replay-status-${symbol}`);
    const selected = state.selected();
    const index = selected ? state.points.indexOf(selected) : 0;
    slider.max = String(Math.max(0, state.points.length - 1));
    slider.value = String(Math.max(0, index));
    slider.disabled = state.points.length < 2;
    live.disabled = state.points.length === 0;
    label.textContent = selected ? formatET(selected.captured_at) : "No current-session snapshots";
    slider.setAttribute("aria-valuetext", label.textContent);
    if (!state.points.length) {
      status.textContent = "No current-session captures · collector status unknown · live feed remains available.";
    } else if (state.mode === "replay") {
      status.textContent = `${index + 1} of ${state.points.length} actual captures · gaps are not interpolated`;
    } else {
      const age = selected ? Date.now() - selected.time : NaN;
      status.textContent = `${state.points.length} actual captures · newest available selected${age > STALE_MS ? " · latest capture is stale" : ""}`;
    }
  }

  async function draw(symbol, point) {
    const state = states[symbol];
    if (!point || inHistory(symbol)) return;
    if (rendered[symbol] === point.captured_at && host.chartStates[symbol].data) return;
    const epoch = ++requestEpoch[symbol];
    const url = `/api/live-replay/${symbol}/snapshot?captured_at=${encodeURIComponent(point.captured_at)}`;
    try {
      let data = snapshotCache[symbol].get(point.captured_at);
      if (!data) {
        const response = await fetch(url, { cache: "no-store", credentials: "same-origin" });
        data = await response.json();
        if (!response.ok || data.error) throw new Error(data.error || "Replay snapshot unavailable");
        snapshotCache[symbol].set(point.captured_at, data);
        if (snapshotCache[symbol].size > 8) snapshotCache[symbol].delete(snapshotCache[symbol].keys().next().value);
      }
      if (epoch !== requestEpoch[symbol] || state.selectedStamp !== point.captured_at || inHistory(symbol)) return;
      if (Date.parse(data.captured_at) !== point.time) throw new Error("Replay timestamp mismatch");
      host.renderResult(data, host.chartStates[symbol]);
      rendered[symbol] = point.captured_at;
      const age = Math.max(0, Date.now() - point.time);
      byId(`chart-mode-${symbol}`).textContent = state.mode === "replay" ? "REPLAY — recorded" : "LIVE — recorded";
      byId(`chart-status-${symbol}`).textContent = `${state.mode === "replay" ? "Replaying" : "Newest capture"} ${formatET(data.captured_at)} · spot ${data.spot}${age > STALE_MS ? " · stale capture" : ""}`;
      updateControls(symbol);
    } catch (error) {
      if (epoch === requestEpoch[symbol]) {
        byId(`live-replay-status-${symbol}`).textContent = `Replay snapshot unavailable: ${error.message}`;
      }
    }
  }

  async function refreshSymbol(symbol) {
    const state = states[symbol];
    const epoch = ++pollEpoch[symbol];
    try {
      const query = state.cursor ? `?after=${encodeURIComponent(state.cursor)}` : "";
      const response = await fetch(`/api/live-replay/${symbol}/timeline${query}`, {
        cache: "no-store", credentials: "same-origin"
      });
      const value = await response.json();
      if (!response.ok || value.error) throw new Error(value.error || "Replay timeline unavailable");
      if (epoch !== pollEpoch[symbol] || inHistory(symbol)) return null;
      const { changedSession } = state.merge(value);
      if (changedSession) {
        rendered[symbol] = null;
        snapshotCache[symbol].clear();
      }
      updateControls(symbol);
      const selected = state.selected();
      if (selected && state.mode === "live") {
        if (rendered[symbol] !== selected.captured_at) await draw(symbol, selected);
        else updateControls(symbol);
      }
      return value;
    } catch (error) {
      if (epoch === pollEpoch[symbol]) byId(`live-replay-status-${symbol}`).textContent = `Replay timeline unavailable: ${error.message}`;
      return null;
    }
  }

  async function refreshAll() {
    await Promise.all(SYMBOLS.filter(symbol => !inHistory(symbol)).map(refreshSymbol));
  }

  function syncSymbol(symbol) {
    const historical = inHistory(symbol);
    byId(`live-replay-${symbol}`).hidden = historical;
    if (!historical) updateControls(symbol);
  }

  for (const symbol of SYMBOLS) {
    byId(`live-replay-time-${symbol}`).addEventListener("input", event => {
      clearTimeout(drawTimers[symbol]);
      requestEpoch[symbol]++;
      const selected = states[symbol].select(Number(event.currentTarget.value));
      updateControls(symbol);
      if (selected) drawTimers[symbol] = setTimeout(() => draw(symbol, selected), 60);
    });
    byId(`live-replay-live-${symbol}`).addEventListener("click", () => {
      clearTimeout(drawTimers[symbol]);
      const selected = states[symbol].goLive();
      updateControls(symbol);
      if (selected) draw(symbol, selected);
    });
  }
  window.GEXLiveReplay = {
    ownsChart: symbol => !inHistory(symbol) && (states[symbol]?.points.length || 0) > 0 &&
      (states[symbol].mode === "replay" || rendered[symbol] !== null),
    refreshAll, refreshSymbol, syncSymbol, states
  };
  SYMBOLS.forEach(syncSymbol);
  refreshAll();
})();
