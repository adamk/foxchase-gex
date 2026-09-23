"use strict";

const assert = require("node:assert/strict");
const {resolveStrikeDomain, resolveScaleRange} = require("../deploy/production-ui/app.js");

const state = {symbol: "NDX"};

function domain(rows, spot = 30300) {
  return resolveStrikeDomain({spot, strikes: rows}, state);
}

// Valid but negligible remote rows must not stretch the NDX viewport.
const focused = domain([
  {strike: "29900", gex: 2},
  {strike: 30000, gex: -8},
  {strike: 30100, gex: 22},
  {strike: 30200, gex: -12},
  {strike: 30300, gex: 10},
  {strike: 30400, gex: 7},
  {strike: 30900, gex: 3},
  {strike: 32100, gex: 0},
  {strike: 34000, gex: 0.001}
]);
assert.ok(focused[0] < 30300 && focused[1] > 30300);
assert.ok(focused[1] < 32100, `remote zero-GEX strike leaked into ${focused}`);

// A distant material concentration must remain visible.
const materialRemote = domain([
  {strike: 30000, gex: 1},
  {strike: 30300, gex: 2},
  {strike: 32100, gex: 20}
]);
assert.ok(materialRemote[1] > 32100, `material remote strike clipped by ${materialRemote}`);

const individuallyMaterialRemote = domain([
  {strike: 30000, gex: 2},
  {strike: 30300, gex: 42.5},
  {strike: 30900, gex: 1.7},
  {strike: 32100, gex: 0}
]);
assert.ok(individuallyMaterialRemote[1] > 30900);

// Numeric ordering is independent of input order and padding is nonzero.
const reordered = domain([
  {strike: 30400, gex: 3},
  {strike: "29900", gex: 2},
  {strike: 30300, gex: 10},
  {strike: 30100, gex: 22}
]);
assert.ok(reordered[0] < 29900 && reordered[1] > 30400);

// SPX keeps its existing autorange, and X-axis scale behavior is unchanged.
assert.equal(resolveStrikeDomain({spot: 7750, strikes: [{strike: 7700, gex: 10}]}, {symbol: "SPX"}), null);
assert.deepEqual(resolveScaleRange({strikes: [{gex: -10}, {gex: 5}]}, {scale: "100"}), [-100, 100]);

console.log("NDX_VIEWPORT_PASS");
