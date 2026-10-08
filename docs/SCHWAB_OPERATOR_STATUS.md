# Schwab operator status

The GEX charts and GEX APIs remain public. Only these application routes
require the separate GEX operator session:

- GET and POST /operator/login
- GET and POST /operator/logout
- GET /api/operator/auth-status
- GET /operator/reauthorize-schwab

Configure GEX_OPERATOR_USERNAME, GEX_OPERATOR_PASSWORD_HASH, and
GEX_SESSION_SECRET in the service environment. The application has no
credentials or fallback secret. If any value is missing, the public chart
continues to load and operator routes fail closed with HTTP 503.

Generate a password hash interactively with the installed Python environment:

    python -c 'from getpass import getpass; from werkzeug.security import generate_password_hash; print(generate_password_hash(getpass()))'

Generate a session secret with:

    python -c 'import secrets; print(secrets.token_urlsafe(48))'

Store only the resulting hash and random session secret in the protected service
environment. Do not put the plaintext password in a unit file or static asset.
Production uses a Secure, HttpOnly, SameSite=Lax cookie named
gex_operator_session, with a 12-hour lifetime. The local GEX_ENV=development
setting disables the Secure flag only for local HTTP review. Login and logout
forms use CSRF tokens, and a successful login clears the pre-authentication
cookie session before issuing a new signed session.

Schwab lifecycle data is stored separately from tokens in a mode-0600 JSON file
containing exactly last_full_oauth_at and next_reauth_due_at. If the file is
absent, the operator status endpoint can migrate those timestamps from the
older nonsecret auth-health record. It never uses access-token refresh time.
Only a successful full authorization-code exchange resets the seven-day
deadline; the existing invalid-grant latch keeps the status at reauthorization
required until the existing authenticated validation succeeds.

The authorization link invokes the existing authorization_url() helper and
redirects to Schwab. The app does not implement a callback or exchange the
authorization code. The authorization URL is not included in HTML or the
status JSON.

The current application has no installed Flask rate-limiting extension or
shared application-level rate-limit store, so this change does not add a
process-local limiter that would behave inconsistently across Gunicorn workers.
Before exposing `/operator/login` publicly, configure and verify an ingress-level
shared rate limit for that path.
