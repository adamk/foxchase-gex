import ipaddress
import os


def _is_loopback_host(value: str) -> bool:
    if value.strip().lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(value.strip()).is_loopback
    except ValueError:
        return False


def _configure_local_launch(host: str) -> None:
    if _is_loopback_host(host):
        os.environ.setdefault("GEX_ENV", "development")
        os.environ.setdefault("GEX_LOCAL_SETUP_ENABLED", "1")
    else:
        os.environ["GEX_ENV"] = "production"
        os.environ["GEX_LOCAL_SETUP_ENABLED"] = "0"


if __name__ == "__main__":
    host = os.getenv("FOXCHASE_GEX_HOST", "127.0.0.1")
    _configure_local_launch(host)

    from gex_client.app import app

    app.run(
        host=host,
        port=app.config["FOXCHASE_GEX_PORT"],
        debug=False,
    )
else:
    from gex_client.app import app
