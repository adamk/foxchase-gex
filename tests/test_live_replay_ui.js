const assert = require("node:assert/strict");

class Element {
  constructor(id) {
    this.id = id; this.hidden = false; this.disabled = false;
    this.value = id.startsWith("history-session-") ? "" : "0";
    this.max = "0"; this.textContent = ""; this.handlers = {};
  }
  addEventListener(name, fn) { (this.handlers[name] ||= []).push(fn); }
  setAttribute() {}
  fire(name) { for (const fn of this.handlers[name] || []) fn({ currentTarget: this }); }
}

const elements = new Map();
global.document = { getElementById: id => elements.get(id) || (elements.set(id, new Element(id)), elements.get(id)) };
const states = { SPX: { symbol: "SPX", data: null }, NDX: { symbol: "NDX", data: null } };
global.window = { GEXReplayHost: { chartStates: states, renderResult: (data, state) => { state.data = data; } } };

const t0 = "2026-10-29T09:30:15-04:00";
const t1 = "2026-10-29T09:31:15-04:00";
const t2 = "2026-10-29T09:32:15-04:00";
let added = false;
const point = (cursor, captured_at) => ({ cursor, captured_at, record_sha256: `h${cursor}` });
global.fetch = async url => {
  const parsed = new URL(url, "http://localhost");
  const symbol = parsed.pathname.split("/")[3];
  if (parsed.pathname.endsWith("/timeline")) {
    let points = symbol === "SPX" ? [point(1, t0), point(2, t1)] : [point(1, t0)];
    if (symbol === "SPX" && added && parsed.searchParams.has("after")) points = [point(3, t2)];
    return { ok: true, json: async () => ({ symbol, session_date: "2026-10-29", points }) };
  }
  const captured_at = parsed.searchParams.get("captured_at");
  const spot = captured_at === t0 ? 6800 : captured_at === t1 ? 6810 : 6820;
  return { ok: true, json: async () => ({ display_symbol: symbol, captured_at, spot, strikes: [{ strike: spot, gex: spot / 10 }] }) };
};

require("../gex_client/static/js/live-replay.js");
const tick = () => new Promise(resolve => setTimeout(resolve, 0));
const debounce = () => new Promise(resolve => setTimeout(resolve, 70));

(async () => {
  await tick(); await tick();
  assert.equal(states.SPX.data.captured_at, t1);
  assert.equal(states.NDX.data.captured_at, t0);
  const slider = elements.get("live-replay-time-SPX");
  slider.value = "0";
  slider.fire("input");
  await debounce();
  assert.equal(states.SPX.data.spot, 6800);
  assert.equal(window.GEXLiveReplay.states.SPX.mode, "replay");
  added = true;
  await window.GEXLiveReplay.refreshAll();
  assert.equal(states.SPX.data.captured_at, t0);
  elements.get("live-replay-live-SPX").fire("click");
  await tick();
  assert.equal(states.SPX.data.captured_at, t2);
  assert.equal(window.GEXLiveReplay.states.SPX.mode, "live");
  elements.get("history-session-SPX").value = "2026-10-28";
  window.GEXLiveReplay.syncSymbol("SPX");
  assert.equal(elements.get("live-replay-SPX").hidden, true);
  console.log("LIVE_REPLAY_UI_PASS");
})().catch(error => { console.error(error); process.exitCode = 1; });
