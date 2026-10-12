"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(
  path.join(__dirname, "../gex_client/static/js/app.js"),
  "utf8"
);

function makeStorage() {
  const values = new Map();
  return {
    getItem(key) {
      return values.has(key) ? values.get(key) : null;
    },
    setItem(key, value) {
      values.set(key, String(value));
    },
    values
  };
}

function extract(pattern, description) {
  const match = source.match(pattern);
  assert.ok(match, `production JavaScript contains ${description}`);
  return match[0];
}

async function main() {
  const sessionInit = extract(
    /const sessionKey = "foxchase-gex-browser-session";\n\s*sessionId = sessionStorage\.getItem\(sessionKey\);[\s\S]*?\n\s*\}/,
    "sessionStorage-backed browser ID initialization"
  );
  assert.match(sessionInit, /crypto\.randomUUID\(\)/);
  assert.match(sessionInit, /sessionStorage\.setItem\(sessionKey, sessionId\)/);
  assert.doesNotMatch(sessionInit, /localStorage/);
  assert.match(source, /setInterval\(heartbeat, 30_000\)/);
  assert.match(source, /\n\s*heartbeat\(\);/);

  let generated = 0;
  const firstTabStorage = makeStorage();
  const initializeTab = storage => {
    const context = vm.createContext({
      sessionId: "",
      sessionStorage: storage,
      crypto: {
        randomUUID() {
          generated += 1;
          return `browser-session-${generated}`;
        }
      }
    });
    vm.runInContext(sessionInit, context);
    return context.sessionId;
  };

  const firstTabId = initializeTab(firstTabStorage);
  assert.equal(firstTabId, "browser-session-1");
  assert.equal(initializeTab(firstTabStorage), firstTabId);
  assert.equal(generated, 1);

  // Independent top-level browsing contexts have separate sessionStorage areas.
  const secondTabId = initializeTab(makeStorage());
  assert.equal(secondTabId, "browser-session-2");
  assert.notEqual(secondTabId, firstTabId);

  const dollarHelper = extract(
    /^const \$ = id => document\.getElementById\(id\);$/m,
    "DOM lookup helper"
  );
  const heartbeat = extract(
    /^async function heartbeat\(\) \{[\s\S]*?^\}/m,
    "presence heartbeat"
  );
  const activeSessions = {textContent: ""};
  const context = vm.createContext({
    sessionId: secondTabId,
    document: {
      getElementById(id) {
        assert.equal(id, "active-sessions");
        return activeSessions;
      }
    },
    fetch: async (url, options) => {
      assert.equal(url, "/api/presence");
      assert.equal(options.method, "POST");
      assert.equal(options.headers["X-GEX-Session"], secondTabId);
      return {ok: true, json: async () => ({online: 5})};
    }
  });

  await vm.runInContext(`${dollarHelper}\n${heartbeat}\nheartbeat()`, context);
  assert.equal(activeSessions.textContent, "5");
  console.log("PRESENCE_UI_PASS");
}

main().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
