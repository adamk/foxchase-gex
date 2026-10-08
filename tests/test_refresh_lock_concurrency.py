import json
import io
import logging
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from gex_client import schwab


def configure(monkeypatch, tmp_path):
    token_path = tmp_path / "tokens.json"
    monkeypatch.setenv("SCHWAB_TOKEN_PATH", str(token_path))
    monkeypatch.setenv("SCHWAB_AUTH_HEALTH_PATH", str(tmp_path / "health.json"))
    monkeypatch.setenv("SCHWAB_CLIENT_ID", "test-client")
    monkeypatch.setenv("SCHWAB_CLIENT_SECRET", "test-client-secret")
    return token_path


def write_expired_token(token_path):
    token_path.write_text(
        json.dumps(
            {
                "access_token": "expired-access-sentinel",
                "refresh_token": "old-refresh-sentinel",
                "expires_in": 1800,
                "saved_at": 0,
            }
        ),
        encoding="utf-8",
    )


class TokenHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        with self.server.counter_lock:
            self.server.refresh_posts += 1
        time.sleep(self.server.response_delay)
        body = json.dumps(
            {
                "access_token": "fresh-access-sentinel",
                "refresh_token": "fresh-refresh-sentinel",
                "expires_in": 1800,
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


def start_token_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), TokenHandler)
    server.refresh_posts = 0
    server.counter_lock = threading.Lock()
    server.response_delay = 0.25
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_two_processes_recheck_freshness_after_shared_lock(monkeypatch, tmp_path):
    token_path = configure(monkeypatch, tmp_path)
    write_expired_token(token_path)
    barrier_dir = tmp_path / "workers-ready"
    barrier_dir.mkdir()
    server, server_thread = start_token_server()
    repo_root = Path(__file__).resolve().parents[1]
    worker = """
import os
import time
from pathlib import Path
from gex_client import schwab

schwab.TOKEN_URL = os.environ["FOXCHASE_TEST_TOKEN_URL"]
initial = schwab.load_tokens()
if schwab._access_token_is_fresh(initial):
    raise SystemExit("test precondition failed: token was fresh")
ready = Path(os.environ["FOXCHASE_TEST_READY_DIR"])
(ready / str(os.getpid())).write_text("ready", encoding="utf-8")
deadline = time.monotonic() + 10
while len(list(ready.iterdir())) < 2:
    if time.monotonic() >= deadline:
        raise SystemExit("workers did not reach the refresh barrier")
    time.sleep(0.01)
schwab._refresh_tokens(initial)
"""
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": str(repo_root),
            "SCHWAB_TOKEN_PATH": str(token_path),
            "SCHWAB_AUTH_HEALTH_PATH": str(tmp_path / "health.json"),
            "SCHWAB_CLIENT_ID": "test-client",
            "SCHWAB_CLIENT_SECRET": "test-client-secret",
            "FOXCHASE_TEST_TOKEN_URL": f"http://127.0.0.1:{server.server_port}/token",
            "FOXCHASE_TEST_READY_DIR": str(barrier_dir),
        }
    )
    processes = []
    try:
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", worker],
                cwd=repo_root,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(2)
        ]
        outputs = [process.communicate(timeout=20) for process in processes]
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)

    assert [process.returncode for process in processes] == [0, 0], outputs
    assert server.refresh_posts == 1
    logs = "\n".join(stderr for _stdout, stderr in outputs)
    assert logs.count("event=refresh_request_initiated") == 1
    assert logs.count("event=refresh_skipped_after_lock") == 1
    assert all(
        "pid=" in line and "timestamp=" in line
        for line in logs.splitlines()
        if "schwab_refresh" in line
    )
    for sentinel in (
        "test-client-secret",
        "old-refresh-sentinel",
        "fresh-refresh-sentinel",
        "fresh-access-sentinel",
    ):
        assert sentinel not in logs
    stored = json.loads(token_path.read_text(encoding="utf-8"))
    assert stored["access_token"] == "fresh-access-sentinel"
    assert stored["refresh_token"] == "fresh-refresh-sentinel"
    assert token_path.with_name("tokens.json.lock").stat().st_mode & 0o777 == 0o600


def test_still_expired_after_lock_posts_once(monkeypatch, tmp_path):
    token_path = configure(monkeypatch, tmp_path)
    write_expired_token(token_path)
    server, server_thread = start_token_server()
    monkeypatch.setattr(
        schwab, "TOKEN_URL", f"http://127.0.0.1:{server.server_port}/token"
    )
    try:
        original = schwab.load_tokens()
        result = schwab._refresh_tokens(original)
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)
    assert result["access_token"] == "fresh-access-sentinel"
    assert server.refresh_posts == 1


def test_refresh_failure_preserves_existing_token_bytes(monkeypatch, tmp_path):
    token_path = configure(monkeypatch, tmp_path)
    write_expired_token(token_path)
    before = token_path.read_bytes()

    class FailedResponse:
        ok = False
        status_code = 400
        text = '{"error":"invalid_grant"}'

        def json(self):
            return {"error": "invalid_grant"}

    calls = []

    def post(*_args, **_kwargs):
        calls.append(1)
        return FailedResponse()

    monkeypatch.setattr(schwab.requests, "post", post)
    log_stream = io.StringIO()
    monkeypatch.setattr(
        schwab._REFRESH_LOGGER,
        "handlers",
        [logging.StreamHandler(log_stream)],
    )
    with pytest.raises(schwab.SchwabError, match="invalid_grant"):
        schwab._refresh_tokens(schwab.load_tokens())
    assert token_path.read_bytes() == before
    assert calls == [1]
    log_output = log_stream.getvalue()
    assert "event=refresh_failed" in log_output
    assert "http_status=400" in log_output
    assert "error_class=invalid_grant" in log_output
    assert "old-refresh-sentinel" not in log_output


def test_token_replacement_remains_atomic_and_private(monkeypatch, tmp_path):
    token_path = configure(monkeypatch, tmp_path)
    token_path.write_text('{"access_token":"old"}\n', encoding="utf-8")
    previous_inode = token_path.stat().st_ino

    schwab.save_tokens(
        {
            "access_token": "fresh-access-sentinel",
            "refresh_token": "fresh-refresh-sentinel",
            "expires_in": 1800,
        }
    )

    assert token_path.stat().st_ino != previous_inode
    assert token_path.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob(".schwab_tokens.*"))


def test_valid_access_token_does_not_refresh(monkeypatch, tmp_path):
    token_path = configure(monkeypatch, tmp_path)
    schwab.save_tokens(
        {
            "access_token": "valid-access-sentinel",
            "refresh_token": "valid-refresh-sentinel",
            "expires_in": 1800,
        }
    )
    monkeypatch.setattr(
        schwab.requests,
        "post",
        lambda *_a, **_k: pytest.fail("valid token unexpectedly refreshed"),
    )

    assert schwab.get_access_token() == "valid-access-sentinel"
