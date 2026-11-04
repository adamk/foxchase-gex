# Historical archive schema

## Current frame format

`gex_client.archive` writes one JSON object per line under
`{FOXCHASE_GEX_DATA_DIR}/{SPX|NDX}/YYYY-MM-DD.jsonl`. The current schema version
is **2**. A frame contains:

| Field | Meaning |
| --- | --- |
| `schema_version` | Archive contract version (`2`). |
| `captured_at` | Capture time with timezone; archive filenames use the New York session date. |
| `request_timestamp`, `response_timestamp` | Local timestamps around the private calculation request; nullable for older/manual records. |
| `symbol` | `SPX` or `NDX`, matching the directory and result. |
| `frame_index` | Zero-based position for the session, assigned by the archive writer. |
| `causal_input` | Sanitized, minimized option-chain snapshot sent to the calculation API. |
| `provenance` | Provider and local source/configuration hashes; includes `input_content_hash` when causal input is present. |
| `result` | Computed response used by the local dashboard and replay. Must contain a `strikes` list. |
| `frame_content_hash` | SHA-256 over the canonical JSON object after removing this field. |

The causal input includes market-derived fields such as symbol, underlying spot,
expiration, option type, strike, open interest, gamma, volatility fallback, and
contract multiplier. It is not the complete Schwab response, but minimization
does not by itself grant redistribution rights. Do not publish archived inputs.

The `result` structure is owned by the private API. Examples in this repository
are synthetic and do not define or implement its calculation methodology.

## Hash validation

The writer serializes the object without `frame_content_hash` using sorted JSON
keys, compact `,` and `:` separators, UTF-8, and `allow_nan=False`, then stores
the lowercase SHA-256 digest. `provenance.input_content_hash` uses the same
canonical encoding for `causal_input`. Verification checks both hashes and
never changes the source line. A party able to rewrite a frame can also
recompute its hash, so the hash is an integrity check, not an authenticity
signature.

The writer counts nonblank lines when assigning the next `frame_index`. A
truncated line, duplicate index, or concurrent-writer index collision is
reported; verification never renumbers or repairs the file. `frame_index` is
read before the writer lock, so concurrent collection calls can collide even
inside one process; verification checks the resulting order explicitly.

## JSONL rules

- A complete record occupies one UTF-8 line ending in a newline.
- Each filename is a valid `YYYY-MM-DD.jsonl`; each record timestamp's New York
  date and symbol must match the file path.
- `frame_content_hash` must match the complete frame body.
- Duplicate timestamps/indexes, backward timestamps, missing frame indexes,
  malformed records, and missing cadence intervals are findings.
- Missing cadence is an availability gap, not a corrupt frame. No timestamp or
  GEX value is synthesized to fill it.
- Weekends, configured holidays, early closes, and intervals outside the
  configured collection window are treated as noncollection periods. Provide a
  reviewed calendar file when whole missing sessions must be distinguished from
  outages; the verifier does not guess an exchange calendar.

Run `python -m gex_client.history_verify --help` for scan bounds and calendar
options. Example calendar files must be stored outside the archive tree.
