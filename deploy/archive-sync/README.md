# Private archive mirroring on Linux

These examples use the archive sync CLI to populate a separate, source-scoped
mirror. The mirror is independent of the live collector and public historical
publisher. Archive bundles include original records. Use this mechanism only
between storage hosts authorized to receive those records.

Replace the example paths, UUIDs, account names and documentation IP with
verified site values. Provision the destination service account with write
access to the mirror and read-only access to the comparison archives. Keep the
archive-export account separate from the collector's credential and token
directories.

Install the exporter and its Python entry point under administrator ownership.
The Python entry point is the standalone `gex_client/archive_sync.py` module.
Authorize a dedicated public key with a restricted forced command, for example
`restrict,command="/usr/local/libexec/gex-backup-exporter"`. Pin the exporter
host key independently in a dedicated known-hosts file. The service account
needs read access to that file and exclusive access to its own private key.

Render and verify the service and timer examples. Confirm both required mounts
match their pinned UUIDs and run the service once to verify the mirror and
journal before enabling the timer. Inspect the service result and journal if
mount verification, SSH, or bundle integrity checks fail.
