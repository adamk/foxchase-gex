# Historical verification and local publishing

## Verify and report archive health

The verifier opens files read-only and reports schema/hash issues, malformed or
truncated records, duplicate captures and indexes, out-of-order timestamps,
missing frame indexes, cadence gaps, session coverage, storage bytes, and
per-symbol latest captures. It reports gaps without inventing missing snapshots.

```bash
python -m gex_client.history_verify examples/synthetic_archive \
  --calendar examples/synthetic_calendar.json --cadence-seconds 60 --format text

python -m gex_client.history_verify "$ARCHIVE_DIR" \
  --required-mount "$ARCHIVE_MOUNT" --cadence-seconds 60 --format json
```

The CLI exit statuses are `0` for a clean scan, `1` for integrity findings or
incomplete coverage, and `2` when the scan cannot be completed or the path is
unsafe/unavailable. Default limits are 10,000 session files, 2,000,000 records,
256 MiB per file, 16 MiB per line, and 2 GiB total archive bytes. Raise a bound
explicitly for a larger archive; a limit hit returns status `unavailable`.

The health section reports the latest valid SPX and NDX captures, available
session dates, cadence gaps, invalid/incomplete sessions, JSONL storage bytes,
and integrity/redistribution readiness. These JSONL bytes cover the SPX/NDX
session files, not the collector's `audit/` and `status/` sidecars.

For whole-day coverage checks, pass an operator-reviewed JSON calendar with
`trading_sessions`, `holidays`, and optional `early_closes` (`YYYY-MM-DD` to
`HH:MM`). Without that calendar the tool does not claim that a weekday with no
archive is a missed trading session. Same-day gaps inside the configured
collection window are reported as possible outage/capture gaps. Weekend,
holiday, early-close, and off-window time is excluded from cadence-gap counts.

## Generate a local artifact set

Begin with the checked-in synthetic sessions. The dry run validates source and
rights boundaries and creates no output directory:

```bash
python -m gex_client.history_export \
  --archive-dir examples/synthetic_archive \
  --output-dir ./build/history \
  --calendar examples/synthetic_calendar.json \
  --allow-synthetic-fixtures \
  --dry-run --format text
```

The synthetic markers are not authenticated. `--allow-synthetic-fixtures` is an
explicit operator opt-in for known fabricated fixtures; do not use it for
arbitrary archives. The export report and manifest label that input
`synthetic_claim_unverified`. To publish the checked-in examples locally,
repeat the command without `--dry-run`. The output layout is:

```text
build/history/
  current.json
  generations/<content-id>/
    index.json
    timelines/SPX/YYYY-MM-DD.jsonl
    timelines/NDX/YYYY-MM-DD.jsonl
```

`current.json` is replaced atomically only after the complete generation is
written, fsynced, and verified. Its index contains symbol/date, capture count,
first/last timestamps, schema version, integrity status, gap count, timeline
path/size, and timeline digest. Timeline paths are relative to the generation
directory. Content IDs and artifact bytes are deterministic; reruns reuse an
identical generation. A rerun can activate a complete generation left behind if
the process stopped before updating `current.json`. Old content-addressed
generations are retained so existing readers keep a valid pointer; a hard
process stop can also leave an unused directory under `.staging/`. Review local
storage before manually removing unreferenced generations or staging data.

The exporter projects only `display_symbol`, `spot`, `strikes`, and `structure`;
strike rows retain only `strike`, `gex`, `call_gex`, and `put_gex`, while
`structure` retains only `net_gex`, `gamma_flip`, `call_wall`, and `put_wall`.
It excludes `causal_input`, provenance, additional result fields, API/session
fields, tokens, account data, and unknown result keys. This supports the
historical GEX chart without publishing raw inputs or unapproved fields.

For a non-synthetic archive, the exporter refuses to write unless the operator
provides a local attestation that redistribution was reviewed. Keep that file
outside Git and outside the archive/output directories. Example shape:

```json
{
  "policy_version": 1,
  "redistribution_review": "operator_attested_not_legal_clearance",
  "result_fields": ["display_symbol", "spot", "strikes", "structure"]
}
```

The policy is an operator gate only. It is not legal advice, proof of a license,
or authorization from Schwab or an exchange. The generated manifest labels this
state `operator_attested_not_legal_clearance`; it can select only supported
fields and must retain `strikes`. If rights for computed output or underlying
market values are unresolved, do not create the attestation. By default any
cadence gap blocks export;
`--allow-capture-gaps` may be used only when the gap is intentionally accepted,
and the generation records its incomplete integrity status. No upload or remote
publication is implemented.

## Synthetic examples

`examples/generate_synthetic_history.py` regenerates three sessions each for
SPX and NDX, with three timestamped captures per session. Each record follows
schema v2, includes a synthetic causal input and valid hashes, and can be read
by the existing local Historical Session API. No external service, Schwab
credential, or real market data is used.
