# Phase 20 Incidents and Evidence

These procedures preserve evidence and fail closed. They do not authorize manual state
editing, reconciliation bypass, retry, resubmission, or live execution.

## Reconciliation required

1. Pause or retain the reconciliation-required runtime state.
2. Preserve the SQLite store, operational log, checkpoint identity, and process output.
3. Identify the exact client order, broker order, position, and event-sequence evidence.
4. Use only the existing exact-evidence PaperBroker reconciliation workflow.
5. Do not infer external broker truth from a local checkpoint.

**BLOCKED:** authoritative OANDA restart reconciliation is not implemented. An external
uncertainty must not be cleared by this runbook.

## Corrupted store

1. Stop startup/recovery; do not fall back to fresh.
2. Copy the database and associated WAL/SHM files without altering the originals.
3. Record the audited commit, configuration fingerprint, session ID, file hashes, and
   observed stable failure category.
4. Diagnose on a copy. Do not edit checksums, sequences, or schema fields to make the
   store load.

Restore from backup is **UNVERIFIED** until the procedure is rehearsed against an
independently validated backup.

## Provider outage or invalid data

1. Keep execution paused/unavailable.
2. Record provider descriptor, route, mapping fingerprint, query identity, and sanitized
   failure category.
3. Do not retry or select fallback implicitly.
4. Resume only after a new bounded request produces point-in-time-valid data under the
   existing provider contract.

## Broker uncertainty or authentication failure

1. Do not retry or resubmit an uncertain order.
2. Retain the reservation and reconciliation-required/kill-switch state.
3. Preserve submission-attempt and correlation evidence.
4. A proven rejection requires the adapter's authoritative response contract; a
   rejection-shaped contradictory status remains uncertain.

**BLOCKED:** the Phase 19 service does not host OANDA and no external reconciliation
coordinator exists.

## Control secret rotation

1. Gracefully stop the observation service.
2. Create a new local secret file outside the repository and apply host permissions.
3. Update only the operational secret-file reference if its path changes.
4. Restart explicitly using fresh or recover as appropriate.
5. Revoke/remove the old file only after confirming no process still uses it.

The procedure above is **PROVISIONAL**: the complete rotation-through-restart sequence
has not been exercised as one operational test. Centralized revocation and Windows ACL
policy are **UNVERIFIED**.

## Backup

1. Stop the foreground service cleanly before copying state.
2. Preserve the SQLite database plus any WAL/SHM files, logs, operational config,
   dataset/configuration identities, and audited commit.
3. Store file hashes with the backup outside the live state directory.
4. Never include control-secret contents in an audit report or log.

This backup procedure is **PROVISIONAL** until tested on the target filesystem and
backup medium.

## Restore validation

1. Restore into a separate validated local state directory.
2. Verify file hashes and SQLite integrity before starting FXLab.
3. Use explicit `--recover --observe-only`.
4. Confirm compatibility fingerprints and recovered monitoring labels.
5. Keep the original evidence unchanged until the restored session is independently
   accepted.

Restore validation is currently **UNVERIFIED** for production deployment.

## Incident evidence set

Preserve, where available:

- audited system commit and report fingerprint;
- session/runtime IDs and sanitized configuration fingerprint;
- provider/broker descriptors and dataset identity;
- event sequence and checkpoint fingerprint;
- SQLite database/WAL/SHM files and hashes;
- redacted operational logs;
- exact named test or reproduction evidence.

Never preserve credentials in the readiness report, audit ledger, monitoring payload,
checkpoint, incident note, or command transcript.
