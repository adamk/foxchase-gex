import json
import subprocess
from pathlib import Path

from jinja2 import Environment


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


def test_hosted_completion_form_is_rendered_only_for_authenticated_operator():
    template = Environment().from_string(PRODUCTION_HTML.read_text(encoding="utf-8"))
    context = {
        "local_operator_access": False,
        "operator_reauthorization_pending": False,
        "operator_csrf_token": "csrf-test-value",
        "operator_reauthorization_notice": None,
        "schwab_connected": False,
    }
    anonymous = template.render(operator_authenticated=False, **context)
    authenticated = template.render(operator_authenticated=True, **context)
    pending = template.render(
        operator_authenticated=True,
        **{**context, "operator_reauthorization_pending": True},
    )
    success = template.render(
        operator_authenticated=True,
        **{
            **context,
            "operator_reauthorization_notice": {
                "kind": "success",
                "message": "Authorization refreshed. Schwab authentication was validated.",
            },
        },
    )
    local = LOCAL_HTML.read_text(encoding="utf-8")

    assert 'id="operator-reauth-completion"' not in anonymous
    assert 'id="operator-reauth-completion"' not in authenticated
    assert 'id="operator-reauth-completion"' in pending
    assert 'action="/operator/complete-schwab-reauthorization"' in pending
    assert 'name="csrf_token" value="csrf-test-value"' in pending
    assert 'id="operator-reauthorize-restart"' in pending
    assert 'target="_blank"' in pending
    assert 'rel="noopener noreferrer"' in pending
    assert "Schwab reauthorization in progress" in pending
    assert 'name="authorization_response"' in pending
    assert 'placeholder="https://127.0.0.1/?code=..."' in pending
    assert "Schwab Connected ✓" in success
    assert 'id="operator-reauth-completion"' not in success
    assert "operator-reauth-completion" not in local


def test_operator_strip_is_styled_in_both_pages():
    for path in (PRODUCTION_CSS, LOCAL_CSS):
        css = path.read_text(encoding="utf-8")
        assert ".operator-auth-strip" in css
        assert ".operator-auth-link" in css
    production_css = PRODUCTION_CSS.read_text(encoding="utf-8")
    assert ".operator-reauth-completion[hidden]" in production_css
    assert ".operator-reauth-notice--error" in production_css
    assert '#operator-reauthorize-link[data-auth-state="healthy"]' in production_css
    assert '#operator-reauthorize-link[data-auth-state="due_soon"]' in production_css
    assert '#operator-reauthorize-link[data-auth-state="reauth_required"]' in production_css


def test_operator_ui_hides_unauthenticated_and_displays_only_expected_controls():
    for path in (PRODUCTION_JS, LOCAL_JS):
        script = f"""
const assert = require("node:assert/strict");
const ui = require({json.dumps(str(path))});
const hosted = {json.dumps(path == PRODUCTION_JS)};
const isLocal = {json.dumps(path == LOCAL_JS)};
class Element {{
  constructor() {{ this.hidden = true; this.textContent = ""; this.href = ""; this.target = ""; this.rel = ""; this.dataset = {{}}; }}
}}
const elements = new Map([
  ["operator-auth-strip", new Element()],
  ["operator-auth-label", new Element()],
  ["operator-reauthorize-link", new Element()],
  ["operator-reauth-completion", new Element()],
  ["operator-logout-link", new Element()]
]);
global.document = {{getElementById: id => elements.get(id)}};
async function main() {{
  global.fetch = async () => ({{ok: false, status: 401}});
  await ui.loadOperatorAuthStatus();
  assert.equal(elements.get("operator-auth-strip").hidden, true);
  assert.equal(elements.get("operator-auth-label").textContent, "");
  assert.equal(elements.get("operator-reauthorize-link").hidden, true);

  global.fetch = async () => ({{ok: true, status: 200, json: async () => ({{state: "healthy", remaining_seconds: 464400, reauthorization_pending: false}})}});
  await ui.loadOperatorAuthStatus();
  assert.equal(elements.get("operator-auth-strip").hidden, false);
  assert.equal(elements.get("operator-auth-label").textContent, "Schwab auth · 5d 9h remaining");
  assert.equal(elements.get("operator-reauthorize-link").hidden, !hosted);
  assert.equal(elements.get("operator-reauth-completion").hidden, true);
  if (hosted) {{
    assert.equal(elements.get("operator-reauthorize-link").href, "/operator/reauthorize-schwab");
    assert.equal(elements.get("operator-reauthorize-link").dataset.authState, "healthy");
  }}

  global.fetch = async () => ({{ok: true, status: 200, json: async () => ({{state: "due_soon", remaining_seconds: 3600, reauthorization_pending: true}})}});
  await ui.loadOperatorAuthStatus();
  assert.equal(elements.get("operator-auth-label").textContent, "Schwab auth · Reauthorize soon");
  assert.equal(elements.get("operator-reauthorize-link").hidden, false);
  assert.equal(elements.get("operator-reauth-completion").hidden, !hosted);
  assert.equal(elements.get("operator-reauthorize-link").href, "/operator/reauthorize-schwab");
  if (hosted) assert.equal(elements.get("operator-reauthorize-link").dataset.authState, "due_soon");
  assert.equal(elements.get("operator-reauthorize-link").target, "_blank");
  assert.equal(elements.get("operator-reauthorize-link").rel, "noopener noreferrer");

  global.fetch = async () => ({{ok: true, status: 200, json: async () => ({{state: "reauth_required", reauth_required: true, reauthorization_pending: true}})}});
  await ui.loadOperatorAuthStatus();
  assert.equal(elements.get("operator-auth-label").textContent, "Schwab auth · Reauthorization required");
  assert.equal(elements.get("operator-reauthorize-link").hidden, false);
  assert.equal(elements.get("operator-reauth-completion").hidden, !hosted);
  assert.equal(elements.get("operator-reauthorize-link").href, "/operator/reauthorize-schwab");
  if (hosted || isLocal) assert.equal(elements.get("operator-reauthorize-link").dataset.authState, "reauth_required");

  global.fetch = async () => ({{ok: false, status: 401}});
  await ui.loadOperatorAuthStatus();
  assert.equal(elements.get("operator-auth-strip").hidden, true);
  assert.equal(elements.get("operator-auth-label").textContent, "");
  assert.equal(elements.get("operator-reauthorize-link").hidden, true);
  assert.equal(elements.get("operator-reauth-completion").hidden, true);
  assert.equal(elements.get("operator-logout-link").hidden, true);

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
class Element {{ constructor() {{ this.hidden = true; this.textContent = ""; this.href = ""; this.dataset = {{}}; }} }}
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
class Element {{ constructor() {{ this.hidden = true; this.textContent = ""; this.dataset = {{}}; }} }}
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


def test_local_setup_card_is_first_run_only_and_hidden_for_configured_accounts():
    script = f"""
const assert = require("node:assert/strict");
const ui = require({json.dumps(str(LOCAL_JS))});
class Element {{ constructor() {{ this.hidden = true; this.textContent = ""; }} }}
const elements = new Map([
  ["setup-card", new Element()],
  ["setup-state", new Element()],
  ["updated", new Element()],
  ["local-auth-continue", new Element()]
]);
global.document = {{
  body: {{dataset: {{localOperatorAccess: "true"}}}},
  getElementById: id => elements.get(id)
}};
let status = {{
  ready: false, client_id_configured: false,
  client_secret_configured: false, token_configured: false,
  authorization_pending: false
}};
global.fetch = async () => ({{ok: true, status: 200, json: async () => status}});
async function main() {{
  assert.equal(await ui.checkSetup(), false);
  assert.equal(elements.get("setup-card").hidden, false);

  status = {{
    ready: false, client_id_configured: true,
    client_secret_configured: true, token_configured: false,
    authorization_pending: false
  }};
  assert.equal(await ui.checkSetup(), false);
  assert.equal(elements.get("setup-card").hidden, true);

  status = {{
    ready: true, client_id_configured: true,
    client_secret_configured: true, token_configured: true,
    authorization_pending: false
  }};
  assert.equal(await ui.checkSetup(), true);
  assert.equal(elements.get("setup-card").hidden, true);

  global.fetch = async () => {{throw new Error("offline")}};
  assert.equal(await ui.checkSetup(), false);
  assert.equal(elements.get("setup-card").hidden, true);
}}
main().catch(error => {{ console.error(error); process.exitCode = 1; }});
"""
    result = subprocess.run(["node", "-e", script], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_local_lifecycle_controls_setup_card_and_reauthorization_action():
    script = f"""
const assert = require("node:assert/strict");
const ui = require({json.dumps(str(LOCAL_JS))});
class Element {{
  constructor() {{ this.hidden = true; this.textContent = ""; this.href = ""; this.target = ""; this.rel = ""; this.dataset = {{}}; }}
}}
const elements = new Map([
  ["setup-card", new Element()], ["setup-state", new Element()], ["local-auth-continue", new Element()],
  ["operator-auth-strip", new Element()], ["operator-auth-label", new Element()],
  ["operator-reauthorize-link", new Element()], ["operator-logout-link", new Element()]
]);
global.document = {{
  body: {{dataset: {{localOperatorAccess: "true"}}}},
  getElementById: id => elements.get(id)
}};
let lifecycleState = "healthy";
global.fetch = async url => {{
  if (url === "/api/setup-status") return {{ok: true, status: 200, json: async () => ({{
    ready: true, client_id_configured: true, client_secret_configured: true,
    token_configured: true, authorization_pending: false
  }})}};
  return {{ok: true, status: 200, json: async () => ({{
    state: lifecycleState, remaining_seconds: 464400
  }})}};
}};
async function main() {{
  for (const [state, label, linkVisible] of [
    ["healthy", "Schwab auth · 5d 9h remaining", false],
    ["due_soon", "Schwab auth · Reauthorize soon", true],
    ["reauth_required", "Schwab auth · Reauthorization required", true]
  ]) {{
    lifecycleState = state;
    assert.equal(await ui.checkSetup(), true);
    await ui.loadOperatorAuthStatus();
    assert.equal(elements.get("setup-card").hidden, true);
    assert.equal(elements.get("operator-auth-strip").hidden, false);
    assert.equal(elements.get("operator-auth-label").textContent, label);
    assert.equal(elements.get("operator-reauthorize-link").hidden, !linkVisible);
    if (linkVisible) assert.equal(elements.get("operator-reauthorize-link").href, "/operator/reauthorize-schwab");
  }}
}}
main().catch(error => {{ console.error(error); process.exitCode = 1; }});
"""
    result = subprocess.run(["node", "-e", script], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
