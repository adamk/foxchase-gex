from unittest.mock import patch
import importlib
import pytest
from gex_client import auth_lifecycle, schwab

@pytest.mark.parametrize('remaining,state', [(259201,'healthy'),(259200,'due_soon'),(86400,'due_soon'),(0,'reauth_required')])
def test_warning_boundaries(monkeypatch, tmp_path, remaining, state):
    monkeypatch.setenv('SCHWAB_AUTH_LIFECYCLE_PATH', str(tmp_path/'lifecycle.json'))
    monkeypatch.setenv('SCHWAB_AUTH_HEALTH_PATH', str(tmp_path/'health.json'))
    auth_lifecycle.record_full_oauth_authorization(1000000)
    assert auth_lifecycle.operator_status(1000000+604800-remaining)['state'] == state

@pytest.mark.parametrize('callback', [
    'https://evil.example/?code=c&state='+'s'*32,
    'https://127.0.0.1/?code=c&code=d&state='+'s'*32,
    'https://127.0.0.1/?code=c&state='+'s'*32+'#fragment',
])
def test_bad_callback_never_exchanges(monkeypatch, callback):
    monkeypatch.setattr(schwab, '_credentials', lambda: ('id','secret','https://127.0.0.1'))
    with patch.object(schwab.requests, 'post') as post:
        with pytest.raises(schwab.SchwabError):
            schwab.exchange_authorization_response(callback, expected_state='s'*32)
        post.assert_not_called()

def test_proactive_setup_keeps_saved_credentials(monkeypatch):
    appmod=importlib.import_module('gex_client.app')
    monkeypatch.setattr(appmod, '_local_setup_request', lambda: True)
    monkeypatch.setattr(appmod, '_setup_status_payload', lambda: {'ready':True,'authorization_pending':False})
    monkeypatch.setattr(appmod, '_render_setup', lambda: ('saved-credential-flow',200))
    assert appmod.app.test_client().get('/setup').status_code == 200

@pytest.mark.parametrize('relative', ['gex_client/static/js/app.js'])
def test_shared_countdown_boundaries_and_clock_skew(relative):
    import json
    import subprocess
    from pathlib import Path
    path=Path(__file__).parents[1]/relative
    script='''const assert=require('node:assert/strict');
const view=require(PATH).authorizationView;
const now=Date.parse('2026-11-03T12:00:00Z');
for(const [seconds,tier] of [[259201,'healthy'],[259200,'72h'],[86400,'24h'],[0,'required']]) {
 const state=view({state:'healthy',next_reauth_due_at:new Date(now+seconds*1000).toISOString()},now);
 assert.equal(state.tier,tier); assert.ok(state.text.includes('Deadline'));
}
assert.equal(view({next_reauth_due_at:'bad'},now).tier,'unknown');
assert.equal(view({state:'reauth_required',next_reauth_due_at:new Date(now+604800000).toISOString()},now).tier,'required');
// Server duration takes precedence over browser wall-clock skew.
assert.equal(view({next_reauth_due_at:new Date(now).toISOString(),_remaining:300000},now+999999999).tier,'healthy');
'''.replace('PATH',json.dumps(str(path)))
    result=subprocess.run(['node','-e',script],capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr

def test_shared_renderer_generated_in_both_bundles():
    from pathlib import Path
    root=Path(__file__).parents[1]
    shared=(root/'gex_client/static/js/authorization.js').read_text()
    for relative in ('gex_client/static/js/app.js',):
        assert (root/relative).read_text().startswith(shared+'\n')
