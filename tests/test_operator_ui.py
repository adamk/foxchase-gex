import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[1]
PRODUCTION_JS = ROOT / "deploy/production-ui/app.js"
PRODUCTION_HTML = ROOT / "deploy/production-ui/index.html"
PRODUCTION_CSS = ROOT / "deploy/production-ui/style.css"
LOCAL_JS = ROOT / "gex_client/static/js/app.js"
LOCAL_HTML = ROOT / "gex_client/templates/index.html"
LOCAL_CSS = ROOT / "gex_client/static/css/style.css"


def test_operator_strip_markup_is_hidden_by_default_in_both_pages():
    for path in (PRODUCTION_HTML, LOCAL_HTML):
        html = path.read_text(encoding="utf-8")
        assert 'id="operator-auth-strip"' in html
        assert 'id="operator-auth-label"' in html
        assert 'id="operator-reauthorize-link"' in html
        assert 'id="operator-logout-link"' in html
        opening = html.split('id="operator-auth-strip"', 1)[1].split(">", 1)[0]
        assert "hidden" in opening
        assert 'href="/operator/reauthorize-schwab"' in html
        assert "client_id=" not in html
        assert "client_secret" not in html


def test_operator_strip_is_styled_in_both_pages():
    for path in (PRODUCTION_CSS, LOCAL_CSS):
        css = path.read_text(encoding="utf-8")
        assert ".operator-auth-strip" in css
        assert ".operator-auth-link" in css


def test_operator_ui_hides_unauthenticated_and_displays_only_expected_controls():
    for path in (PRODUCTION_JS, LOCAL_JS):
        script = f"""
const assert = require("node:assert/strict");
const ui = require({json.dumps(str(path))});
class Element {{
  constructor() {{ this.hidden = true; this.textContent = ""; this.href = ""; this.target = ""; this.rel = ""; }}
}}
const elements = new Map([
  ["operator-auth-strip", new Element()],
  ["operator-auth-label", new Element()],
  ["operator-reauthorize-link", new Element()],
  ["operator-logout-link", new Element()]
]);
global.document = {{getElementById: id => elements.get(id)}};
async function main() {{
  global.fetch = async () => ({{ok: false, status: 401}});
  await ui.loadOperatorAuthStatus();
  assert.equal(elements.get("operator-auth-strip").hidden, true);
  assert.equal(elements.get("operator-auth-label").textContent, "");
  assert.equal(elements.get("operator-reauthorize-link").hidden, true);

  global.fetch = async () => ({{ok: true, status: 200, json: async () => ({{state: "healthy", remaining_seconds: 464400}})}});
  await ui.loadOperatorAuthStatus();
  assert.equal(elements.get("operator-auth-strip").hidden, false);
  assert.equal(elements.get("operator-auth-label").textContent, "Schwab auth · 5d 9h remaining");
  assert.equal(elements.get("operator-reauthorize-link").hidden, true);

  global.fetch = async () => ({{ok: true, status: 200, json: async () => ({{state: "due_soon", remaining_seconds: 3600}})}});
  await ui.loadOperatorAuthStatus();
  assert.equal(elements.get("operator-auth-label").textContent, "Schwab auth · Reauthorize soon");
  assert.equal(elements.get("operator-reauthorize-link").hidden, false);
  assert.equal(elements.get("operator-reauthorize-link").href, "/operator/reauthorize-schwab");
  assert.equal(elements.get("operator-reauthorize-link").target, "_blank");
  assert.equal(elements.get("operator-reauthorize-link").rel, "noopener noreferrer");

  global.fetch = async () => ({{ok: true, status: 200, json: async () => ({{state: "reauth_required", reauth_required: true}})}});
  await ui.loadOperatorAuthStatus();
  assert.equal(elements.get("operator-auth-label").textContent, "Schwab auth · Reauthorization required");
  assert.equal(elements.get("operator-reauthorize-link").hidden, false);

  global.fetch = async () => ({{ok: false, status: 401}});
  await ui.loadOperatorAuthStatus();
  assert.equal(elements.get("operator-auth-strip").hidden, true);
  assert.equal(elements.get("operator-auth-label").textContent, "");

  let scheduled;
  let visibilityHandler;
  global.setInterval = (callback, delay) => {{ scheduled = {{callback, delay}}; }};
  document.addEventListener = (name, callback) => {{
    if (name === "visibilitychange") visibilityHandler = callback;
  }};
  ui.startOperatorAuthStatusRefresh();
  assert.equal(scheduled.delay, 30000);
  assert.equal(typeof visibilityHandler, "function");
  global.fetch = async () => ({{ok: true, status: 200, json: async () => ({{state: "healthy", remaining_seconds: 100000}})}});
  await scheduled.callback();
  assert.equal(elements.get("operator-auth-strip").hidden, false);
  document.hidden = false;
  global.fetch = async () => ({{ok: false, status: 401}});
  visibilityHandler();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(elements.get("operator-auth-strip").hidden, true);
}}
main().catch(error => {{ console.error(error); process.exitCode = 1; }});
"""
        result = subprocess.run(["node", "-e", script], cwd=ROOT, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr


def test_local_loopback_operator_status_does_not_offer_login_logout(monkeypatch):
    script = f"""
const assert = require("node:assert/strict");
const ui = require({json.dumps(str(LOCAL_JS))});
class Element {{ constructor() {{ this.hidden = true; this.textContent = ""; this.href = ""; }} }}
const elements = new Map([
  ["operator-auth-strip", new Element()],
  ["operator-auth-label", new Element()],
  ["operator-reauthorize-link", new Element()],
  ["operator-logout-link", new Element()]
]);
global.document = {{
  body: {{dataset: {{localOperatorAccess: "true"}}}},
  getElementById: id => elements.get(id)
}};
global.fetch = async () => ({{
  ok: true, status: 200,
  json: async () => ({{state: "due_soon", remaining_seconds: 600}})
}});
ui.loadOperatorAuthStatus().then(() => {{
  assert.equal(elements.get("operator-auth-strip").hidden, false);
  assert.equal(elements.get("operator-reauthorize-link").hidden, false);
  assert.equal(elements.get("operator-logout-link").hidden, true);
}}).catch(error => {{ console.error(error); process.exitCode = 1; }});
"""
    result = subprocess.run(["node", "-e", script], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_local_callback_continuation_tracks_pending_authorization():
    script = f"""
const assert = require("node:assert/strict");
const ui = require({json.dumps(str(LOCAL_JS))});
class Element {{ constructor() {{ this.hidden = true; this.textContent = ""; }} }}
const elements = new Map([
  ["setup-card", new Element()],
  ["setup-state", new Element()],
  ["local-auth-continue", new Element()]
]);
global.document = {{
  body: {{dataset: {{localOperatorAccess: "true"}}}},
  getElementById: id => elements.get(id)
}};
let setupStatus = {{ready: true, authorization_pending: true}};
global.fetch = async () => ({{ok: true, status: 200, json: async () => setupStatus}});
async function main() {{
  assert.equal(await ui.checkSetup(), true);
  assert.equal(elements.get("local-auth-continue").hidden, false);
  assert.equal(elements.get("setup-card").hidden, true);

  setupStatus = {{ready: true, authorization_pending: false}};
  await ui.checkSetup();
  assert.equal(elements.get("local-auth-continue").hidden, true);
}}
main().catch(error => {{ console.error(error); process.exitCode = 1; }});
"""
    result = subprocess.run(["node", "-e", script], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
