import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from gex_client.archive import archive_snapshot, read_day, validate_frame_record
from gex_client.health import record_attempt
from gex_client import schwab

NY = ZoneInfo("America/New_York")


def result():
    return {"display_symbol":"SPX", "spot":7700, "updated_at":"2026-09-01T09:30:00-04:00",
            "strikes":[{"strike":7700,"gex":1.2,"call_gex":2.0,"put_gex":0.8}],
            "structure":{"net_gex":1.2,"gamma_flip":7695,"call_wall":7720,"put_wall":7680,
                         "method_version":"foxchase_structure_v1"}}


def causal():
    return {"symbol":"SPX","spot":7700,"expiration_date":"2026-09-01","contracts":[
        {"option_type":"CALL","strike":7700,"open_interest":10,"gamma":.01,"volatility":.2,"multiplier":100}]}


def test_frame_hash_and_deterministic_stored_replay(monkeypatch, tmp_path):
    monkeypatch.setenv("FOXCHASE_GEX_DATA_DIR", str(tmp_path))
    stamp=datetime(2026,9,1,9,30,tzinfo=NY)
    archive_snapshot("SPX",result(),stamp,causal_input=causal(),provenance={"provider":"Schwab"})
    record=read_day("SPX","2026-09-01")[0]
    assert validate_frame_record(record)
    assert record["result"]["structure"]["net_gex"] == 1.2
    assert record["causal_input"] == causal()
    tampered=json.loads(json.dumps(record)); tampered["result"]["spot"]=1
    assert not validate_frame_record(tampered)


def test_missing_frame_detection_and_alert_debounce(monkeypatch,tmp_path):
    monkeypatch.setenv("FOXCHASE_GEX_DATA_DIR",str(tmp_path))
    monkeypatch.setattr("gex_client.health.FAILURE_THRESHOLD",3)
    now=datetime(2026,9,1,9,35,tzinfo=NY)
    assert record_attempt("SPX",False,now,60,"OAuthError")["alert"] is None
    assert record_attempt("SPX",False,now,60,"OAuthError")["alert"] is None
    third=record_attempt("SPX",False,now,60,"OAuthError")
    assert third["alert"]["transition"] == "initializing->degraded"
    assert record_attempt("SPX",False,now,60,"OAuthError")["alert"] is None
    recovered=record_attempt("SPX",True,now,60)
    assert recovered["alert"]["transition"] == "degraded->healthy"
    assert recovered["state"]["symbols"]["SPX"]["missing_frames"] == 5


class FakeResponse:
    def __init__(self, status, payload):
        self.status_code=status; self._payload=payload; self.ok=status < 400
        self.text=json.dumps(payload)
    def json(self): return self._payload


def test_successful_refresh_is_securely_persisted(monkeypatch,tmp_path):
    monkeypatch.setenv("SCHWAB_CLIENT_ID","configured")
    monkeypatch.setenv("SCHWAB_CLIENT_SECRET","configured")
    monkeypatch.setenv("SCHWAB_TOKEN_PATH",str(tmp_path/"tokens.json"))
    monkeypatch.setattr(schwab.requests,"post",lambda *a,**k: FakeResponse(200,{
        "access_token":"new-access","refresh_token":"new-refresh","expires_in":1800}))
    value=schwab._refresh_tokens({"refresh_token":"old-refresh"})
    assert value["access_token"] == "new-access"
    saved=json.loads((tmp_path/"tokens.json").read_text())
    assert saved["refresh_token"] == "new-refresh"
    assert oct((tmp_path/"tokens.json").stat().st_mode & 0o777) == "0o600"


def test_revoked_or_expired_refresh_fails_without_token_leak(monkeypatch):
    monkeypatch.setenv("SCHWAB_CLIENT_ID","configured")
    monkeypatch.setenv("SCHWAB_CLIENT_SECRET","configured")
    monkeypatch.setattr(schwab.requests,"post",lambda *a,**k: FakeResponse(400,{
        "error":"invalid_grant","error_description":"refresh token invalid"}))
    try:
        schwab._refresh_tokens({"refresh_token":"must-not-appear"})
    except schwab.SchwabError as exc:
        assert "must-not-appear" not in str(exc)
        assert "invalid_grant" in str(exc)
    else: raise AssertionError("invalid refresh was accepted")


def test_expired_access_invokes_refresh(monkeypatch):
    monkeypatch.setattr(schwab,"load_tokens",lambda:{"access_token":"old","refresh_token":"r","saved_at":0,"expires_in":1800})
    monkeypatch.setattr(schwab,"_refresh_tokens",lambda value:{"access_token":"fresh","refresh_token":"r"})
    assert schwab.get_access_token() == "fresh"


def test_bounded_spx_and_ndx_chain_requests(monkeypatch):
    monkeypatch.setattr(schwab,"get_access_token",lambda:"private")
    expiration=datetime.now(NY).date().isoformat()
    calls=[]
    def fake_get(url,headers,params,timeout):
        calls.append((params["symbol"],params["contractType"]))
        side="callExpDateMap" if params["contractType"]=="CALL" else "putExpDateMap"
        return FakeResponse(200,{"underlyingPrice":7700 if params["symbol"]=="$SPX" else 25000,
            side:{f"{expiration}:0":{"7700.0":[{"openInterest":10,"gamma":.01,"volatility":20,"multiplier":100}]}}})
    monkeypatch.setattr(schwab.requests,"get",fake_get)
    assert schwab.fetch_sanitized_snapshot("SPX")["contracts"]
    assert schwab.fetch_sanitized_snapshot("NDX")["contracts"]
    assert {x[0] for x in calls} == {"$SPX","$NDX"}
