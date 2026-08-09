from gex_client.app import app


if __name__ == "__main__":
    app.run(
        host="127.0.0.1",
        port=app.config["FOXCHASE_GEX_PORT"],
        debug=False,
    )

