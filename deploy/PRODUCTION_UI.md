# Foxchase GEX production UI

`deploy/production-ui/` is reviewed source for three UI files served by
production. It stays separate from `gex_client` because production is not a
Git checkout.

## Source mapping

| Repository source | Production target |
| --- | --- |
| `deploy/production-ui/index.html` | `/opt/foxchase-gex/static/templates/index.html` |
| `deploy/production-ui/app.js` | `/opt/foxchase-gex/static/js/app.js` |
| `deploy/production-ui/style.css` | `/opt/foxchase-gex/static/css/style.css` |

On the current Linode, systemd bind-mounts the application tree read-only into
the service namespace. The writable host backing tree is
`/opt/foxchase-authority-runtime/rootfs/opt/foxchase-gex`; the deployment
script writes only the three mapped assets there, which the service sees at
the logical paths in the table. The production baseline pins those backing
files and must match before a deployment.

`SHA256SUMS` verifies reviewed source. `production-baseline.SHA256SUMS` is
expected current production state and provides drift protection. Update that
baseline only as part of separately reviewed UI release.

## Controlled deployment

Run from checked-out repository as root:

```bash
deploy/deploy_production_ui.sh --dry-run
deploy/deploy_production_ui.sh
```

Script:

1. validates reviewed source hashes, exact production paths, and the current
   production asset baseline;
2. runs the health gate against the current production hashes; it validates
   operator OAuth lifecycle first and uses the production `get_access_token()`
   path as `foxchase-gex` only when a healthy lifecycle has a stale access token;
3. creates `/opt/foxchase-gex/ui-backups/<UTC timestamp>/`, copies all three
   current UI files, and verifies the backup `SHA256SUMS`;
4. installs exactly the three mapped UI files, preserving target owner, group,
   and mode;
5. verifies installed hashes against the release manifest and repeats the
   health gate;
6. requires NDX and SPX GEX HTTP 200 during weekday trading validation. On a
   weekend or recognized US equity market holiday, the gate accepts only the
   exact missing-spot response after authenticated Schwab CALL and PUT chain
   requests confirm empty same-day expiry groups. Backend errors, compute
   failures, service failures, auth failures, or nonempty chains still block;
7. restores the verified backup automatically if post-install validation fails.

The health gate checks active GEX and compute services, exactly two live
Gunicorn workers, a stable GEX service PID and restart count during validation,
public and local roots, served CSS/JavaScript, production asset hashes, healthy
Schwab lifecycle metadata and remaining time, an authenticated Schwab quotes
request, and fresh service logs. The current production app does not expose
`/api/health`, so the gate does not depend on that route. A normal access-token
refresh may update the token file when needed; it must preserve owner/mode and
must not reset the full-OAuth lifecycle or clear an auth-failure latch.

Static templates/assets are read from disk per request, so no service restart
normally required. Use `--restart` only when proven runtime/cache reason
requires it; it restarts `foxchase-gex.service` only.

Script never writes root-level `/SHA256SUMS`, manually edits Schwab state, or
changes backend/auth/systemd configuration. Review baseline and backup output
before production deployment.
