# Foxchase security.txt publication

This directory contains public-only RFC 9116 metadata for the GEX host. The
signing key and its revocation certificate are kept separately on
`trading-ec2` under `/home/ubuntu/.local/share/foxchase-security-signing/gnupg`.
Never copy that GnuPG home into this repository or any web root.

The signed files are installed at `/var/www/foxchase-gex-security/.well-known/`.
Lighttpd serves only `security.txt` and `pgp-key.txt` directly for the GEX HTTPS
virtual host; all other requests retain the existing Gunicorn proxy behavior.

`gex-lighttpd-route.conf.snippet` documents the exact URL condition inserted in
the existing GEX HTTPS host blocks. The existing proxy rules remain in their
`else` branches; this snippet is not a standalone virtual host.
