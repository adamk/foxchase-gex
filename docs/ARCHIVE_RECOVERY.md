# Archive backup and read-only recovery

## Primary archive and backup

The Pi's external T7 SSD is the authoritative archive. Keep the collector's
`FOXCHASE_GEX_DATA_DIR` beneath the verified mount and set
`FOXCHASE_GEX_REQUIRED_MOUNT` to that mount. If the mount is absent, do not let
collection fall back to the system disk. Linode indexes or public artifacts are
not a backup of the primary archive.

Maintain a second, independent, access-controlled backup on separate storage.
Use a new empty destination for each recovery copy and leave the source alone.
For example, after an operator has selected and confirmed both local mount
paths, set the placeholders to the actual paths on that host:

```bash
export ARCHIVE_MOUNT=/path/to/mounted-external-ssd
export ARCHIVE_DIR="$ARCHIVE_MOUNT/foxchase-gex/archive"
export BACKUP_DIR=/path/to/independent-backup/foxchase-gex/archive-copy
python -m gex_client.history_verify "$ARCHIVE_DIR" \
  --required-mount "$ARCHIVE_MOUNT" --format text
rsync -a --checksum "$ARCHIVE_DIR/" "$BACKUP_DIR/"
python -m gex_client.history_verify "$BACKUP_DIR" --format text
```

Do not add `--delete` to a recovery copy. Retain the source-side verification
report, backup timestamp, and a separate checksum manifest with the backup.
Protect archive and backup storage according to the Schwab/exchange market-data
terms that apply to the stored causal inputs and computed results.

## Missing or inaccessible T7

If the required mount check fails, stop and establish the storage state through
the authorized local operator procedure. Do not point the collector at an empty
mount directory, create replacement files, or infer that the SSD is empty. The
verifier exits `2` when the archive or required mount is unavailable. Its
`--required-mount` check verifies the mount and confirms the archive path is
beneath it.

If the disk must be examined on a recovery host, mount it read-only using the
host's approved device-identification procedure, then run the verifier against
that read-only mount. Device names and mount commands are environment-specific
and are intentionally not hard-coded here.

## Corruption or incomplete sessions

1. Preserve the original directory and all malformed, truncated, duplicate, or
   out-of-order lines. The verifier never repairs or edits source records.
2. Save the verifier JSON report outside the archive. Re-run only after the
   source has stopped changing or the full scan clearly reports a consistent
   append-only prefix.
3. Compare against a verified independent backup. Restore into a separate
   recovery directory, verify the copy, and only then make a separately reviewed
   configuration change to select that recovery location.
4. Keep outage gaps and early closes visible. Do not interpolate, duplicate, or
   overwrite captures to make a session appear complete.
5. Keep incomplete sessions out of publication unless the operator deliberately
   uses `--allow-capture-gaps`; resulting artifacts identify the gaps.

Use the synthetic sessions under `examples/synthetic_archive/` to rehearse the
verifier, local replay, and exporter without touching production storage.
