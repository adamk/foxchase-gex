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

1. validates source hashes and exact current production baseline;
2. creates `/opt/foxchase-gex/ui-backups/<UTC timestamp>/`;
3. backs up all three files and writes `SHA256SUMS` inside that directory;
4. installs exactly mapped files, preserving target owner/group/mode;
5. checks production site and both GEX API endpoints;
6. restores verified backup automatically if post-install validation fails.

Static templates/assets are read from disk per request, so no service restart
normally required. Use `--restart` only when proven runtime/cache reason
requires it; it restarts `foxchase-gex.service` only.

Script never writes root-level `/SHA256SUMS`, touches Schwab tokens, or changes
backend/auth/systemd configuration. Review baseline and backup output before
production deployment.
