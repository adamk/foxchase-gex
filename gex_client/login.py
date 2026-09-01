from getpass import getpass

from gex_client.auth_health import send_dashboard_status

from gex_client.schwab import (
    SchwabError,
    authorization_url,
    exchange_authorization_response,
    token_path,
)


def main() -> None:
    try:
        print("\nOpen this URL in your browser and authorize your Schwab app:\n")
        print(authorization_url())
        print("\nAfter Schwab redirects, copy the full URL from the address bar.")
        response = getpass("Paste the redirect URL (input hidden): ")
        exchange_authorization_response(response)
        send_dashboard_status(True)
        print(f"\nSchwab authorization saved locally at {token_path()}")
    except SchwabError as exc:
        raise SystemExit(f"Schwab login failed: {exc}") from exc


if __name__ == "__main__":
    main()
