"use strict";

const assert = require("node:assert/strict");
const ui = require("../deploy/production-ui/app.js");

const semanticClasses = [
  "chart-classification--neutral",
  "chart-classification--gamma-pin",
  "chart-classification--mixed",
  "chart-classification--positive",
  "chart-classification--negative"
];

class FakeClassList {
  constructor() {
    this.values = new Set();
  }

  add(...values) {
    values.forEach(value => this.values.add(value));
  }

  remove(...values) {
    values.forEach(value => this.values.delete(value));
  }

  contains(value) {
    return this.values.has(value);
  }
}

class FakeElement {
  constructor() {
    this.hidden = false;
    this.textContent = "";
    this.classList = new FakeClassList();
  }
}

class FakeDocument {
  constructor() {
    this.elements = new Map();
    [
      "error",
      "classification-NDX",
      "classification-SPX",
      "chart-status-NDX",
      "chart-status-SPX",
      "chart-error-NDX",
      "chart-error-SPX"
    ]
      .forEach(id => this.elements.set(id, new FakeElement()));
  }

  querySelectorAll(selector) {
    assert.fail(`unexpected selector lookup: ${selector}`);
  }

  getElementById(id) {
    const element = this.elements.get(id);
    assert.ok(element, `unexpected DOM lookup: ${id}`);
    return element;
  }

}

function assertClassification(document, symbol, primary, semanticClass) {
  const badge = document.getElementById(`classification-${symbol}`);
  assert.equal(ui.classificationToneClass(primary), semanticClass);
  assert.equal(badge.textContent, primary);
  assert.equal(badge.hidden, false);
  assert.equal(badge.classList.contains(semanticClass), true,
    `${symbol} semantic class for ${primary}`);
  for (const otherClass of semanticClasses) {
    if (otherClass !== semanticClass) {
      assert.equal(badge.classList.contains(otherClass), false,
        `${symbol} should not retain ${otherClass}`);
    }
  }
}

async function main() {
  const document = new FakeDocument();
  global.document = document;

  // Each supported primary maps to the existing Gamma Ramp semantic color.
  for (const [primary, semanticClass] of [
    ["Gamma Pin", "chart-classification--gamma-pin"],
    ["Mixed Gamma", "chart-classification--mixed"],
    ["Forward Positive Ramp", "chart-classification--positive"],
    ["Backward Positive Ramp", "chart-classification--positive"],
    ["Forward Negative Ramp", "chart-classification--negative"],
    ["Backward Negative Ramp", "chart-classification--negative"]
  ]) {
    ui.renderClassification({patterns: {primary}}, {symbol: "NDX"});
    assertClassification(document, "NDX", primary, semanticClass);
  }

  // Both headers can show different classifications at the same time.
  ui.renderClassification({patterns: {primary: "Gamma Pin"}}, {symbol: "NDX"});
  ui.renderClassification({patterns: {primary: "Forward Positive Ramp"}}, {symbol: "SPX"});
  assertClassification(document, "NDX", "Gamma Pin", "chart-classification--gamma-pin");
  assertClassification(document, "SPX", "Forward Positive Ramp", "chart-classification--positive");

  // A changed NDX primary does not change the SPX header.
  ui.renderClassification({patterns: {primary: "Mixed Gamma"}}, {symbol: "NDX"});
  assertClassification(document, "NDX", "Mixed Gamma", "chart-classification--mixed");
  assertClassification(document, "SPX", "Forward Positive Ramp", "chart-classification--positive");

  // Secondary signals and verbose read fields cannot create a classification.
  ui.renderClassification({
    read_title: "Forward Positive Ramp",
    patterns: {signals: [{type: "Forward Positive Ramp"}]}
  }, {symbol: "NDX"});
  const missing = document.getElementById("classification-NDX");
  assert.equal(missing.textContent, "");
  assert.equal(missing.hidden, true);
  assert.equal(missing.classList.contains("chart-classification--neutral"), true);

  global.fetch = async url => ({
    ok: false,
    json: async () => ({error: `${url} unavailable`})
  });

  ui.renderClassification({patterns: {primary: "Gamma Pin"}}, {symbol: "NDX"});
  ui.renderClassification({patterns: {primary: "Mixed Gamma"}}, {symbol: "SPX"});
  await ui.loadGex({symbol: "NDX", loadInFlight: false});
  assert.equal(document.getElementById("classification-NDX").hidden, true);
  assert.equal(document.getElementById("classification-NDX").classList.contains("chart-classification--neutral"), true);
  assertClassification(document, "SPX", "Mixed Gamma", "chart-classification--mixed");

  ui.renderClassification({patterns: {primary: "Gamma Pin"}}, {symbol: "NDX"});
  await ui.loadGex({symbol: "SPX", loadInFlight: false});
  assertClassification(document, "NDX", "Gamma Pin", "chart-classification--gamma-pin");
  assert.equal(document.getElementById("classification-SPX").hidden, true);
  assert.equal(document.getElementById("classification-SPX").classList.contains("chart-classification--neutral"), true);

  delete global.fetch;
  delete global.document;
  console.log("DOM_HARNESS_PASS");
}

main().catch(error => {
  delete global.fetch;
  delete global.document;
  console.error(error);
  process.exitCode = 1;
});
