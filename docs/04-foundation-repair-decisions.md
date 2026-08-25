# Foundation Repair Decisions

**Date:** 2026-08-25  
**Status:** Approved foundation clarification  
**Scope:** Execution-layer foundations only; the research layer remains frozen

## Market-data closure watermark

A bar is closed only when:

```text
ts_open + timeframe_delta <= authoritative_time_watermark
```

The newest accepted live tick timestamp is the authoritative watermark when accepted
ticks exist for the stream. When no live ticks exist, the watermark comes from the
injected UTC time provider. Production may use the UTC wall clock, while tests inject a
deterministic time. Broker-supplied historical bars are never assumed to be closed.

The equality boundary is intentional: a bar whose close time exactly equals the
watermark is closed. A later wall-clock value cannot override an older accepted tick
watermark, which prevents time-based lookahead beyond observed live data.

## Historical and real-time overlap

Both broker history and tick-derived bars must pass the same authoritative closure test.
Tick-derived bars fill timestamps absent from proven-closed history. If both sources have
the same `ts_open`, the proven-closed historical OHLCV row wins because a partial tick
buffer must not replace the broker's complete closed bar.

## SignalEngine duplicate scope

Duplicate suppression belongs to each `SignalEngine` instance. Re-polling the same closed
bar on one instance is suppressed, while an independent instance evaluates that bar with
its own state. There is no global, singleton, file, database, cross-process, or restart-
durable duplicate state.

`SignalEvent` remains directional only. Its contract is unchanged and does not carry
entry, stop-loss, or take-profit prices. A future execution-intent layer owns entry,
stop-loss, and take-profit construction.

## Future Phase 4 dependencies and state

Phase 4 has not started. When approved, symbol pip-size lookup will use
`CostConfig.pip_size_for(symbol)`, not `CostModel.pip_size_for(symbol)`.

The first `RiskEngine` version will use in-memory session state only. It will make no
restart-durability claim; persistence and recovery semantics require a separate future
decision and approval.

## Research integrity

These decisions do not alter setups, backtests, costs, configuration, validation,
acceptance criteria, train/validation/test boundaries, ADRs 0001–0005, the experiment
registry, or the `SignalEvent` contract.
