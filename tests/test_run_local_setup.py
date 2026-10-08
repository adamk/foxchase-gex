import importlib


run_module = importlib.import_module("run")


def test_loopback_run_defaults_to_local_gui_setup(monkeypatch):
    monkeypatch.delenv("GEX_ENV", raising=False)
    monkeypatch.delenv("GEX_LOCAL_SETUP_ENABLED", raising=False)

    run_module._configure_local_launch("127.0.0.1")

    assert __import__("os").environ["GEX_ENV"] == "development"
    assert __import__("os").environ["GEX_LOCAL_SETUP_ENABLED"] == "1"


def test_non_loopback_run_disables_gui_credential_setup(monkeypatch):
    monkeypatch.setenv("GEX_ENV", "development")
    monkeypatch.setenv("GEX_LOCAL_SETUP_ENABLED", "1")

    run_module._configure_local_launch("0.0.0.0")

    assert __import__("os").environ["GEX_ENV"] == "production"
    assert __import__("os").environ["GEX_LOCAL_SETUP_ENABLED"] == "0"


def test_loopback_environment_can_explicitly_disable_gui_setup(monkeypatch):
    monkeypatch.delenv("GEX_ENV", raising=False)
    monkeypatch.setenv("GEX_LOCAL_SETUP_ENABLED", "0")

    run_module._configure_local_launch("localhost")

    assert __import__("os").environ["GEX_ENV"] == "development"
    assert __import__("os").environ["GEX_LOCAL_SETUP_ENABLED"] == "0"
