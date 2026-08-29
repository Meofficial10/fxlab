# Phase 20 Observation-Service Operations

**Scope:** local, foreground, observation-only operation.  
**Authority:** this runbook cannot enable a strategy or external broker.

## Preconditions

- Use a validated absolute local state directory and operational TOML.
- Create a local control-secret file containing at least 32 bytes and restrict its
  permissions using the host operating system.
- Supply the existing replay/query inputs.
- Select exactly one of `--fresh` or `--recover`.
- Supply `--observe-only`; the service also installs the no-signal setup,
  `observe-only-v1` decline policy, and PaperBroker internally.

## Fresh foreground startup

1. Preserve any existing state directory; never delete durable evidence to obtain a
   fresh start.
2. Validate the operational config and replay inputs.
3. Run `fxlab service run ... --fresh --observe-only` in the foreground.
4. Confirm status reports the service lifecycle and a `LIVE_RUNTIME` monitoring source.
5. Confirm the runtime, provider, broker, dataset, valuation, margin, and configuration
   identities match the intended observation session.

This procedure is validated for the local observation boundary. It does not start an
OANDA service.

## Recovery startup

1. Preserve the SQLite store, logs, and lock file.
2. Run the same configuration with `--recover --observe-only`.
3. Recovery must reject corrupt, incompatible, stopped, failed, or unresolved state.
4. A recovered prior RUNNING state remains paused/blocked for maintenance; it is not
   execution authorization.
5. Inspect status and durable events before any permitted resume.

There is no automatic fallback from recovery to fresh startup.

## Local controls

- `fxlab service status`: read-only; it must not append operator events or checkpoints.
- `fxlab service pause`: blocks new runtime entries while maintenance remains available.
- `fxlab service resume`: resumes only the no-signal/decline-only observation lifecycle.
- `fxlab service emergency-stop`: latches the existing fail-closed control.
- `fxlab service stop`: requests serialized graceful shutdown.

The authenticated actor comes from operational configuration, never client JSON.

## Graceful stop and evidence preservation

Expected order:

1. Service enters STOPPING.
2. The session receives a serialized stop request.
3. Any cycle already inside the lifecycle gate finishes.
4. Maintenance/reflection drains.
5. Stop completion and a checkpoint are attempted only at a safe point.
6. Control endpoint, store, logger, signal handlers, and instance lock are cleaned up.
7. A critical cleanup failure produces FAILED rather than a false clean STOPPED result.

Retain the SQLite store, operational log, configuration reference, dataset identity,
and exact commit. Do not edit or delete evidence after an incident.

## Limitations

- **BLOCKED:** external/OANDA broker hosting and strategy execution.
- **UNVERIFIED:** hostile multi-user Windows security and mapped-drive locality.
- **PROVISIONAL:** host-specific supervision and log-retention procedures; Phase 19 is
  a foreground process and supplies no daemon manager.

