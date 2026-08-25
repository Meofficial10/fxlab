"""Event-driven, closed-candle-only backtest engine + metrics (Phase 2+). Not in P1.

Custom (not vectorbt/backtrader) so we retain full control over leakage safety,
per-bar cost/slippage/latency, and path-dependent SMC / partial-exit logic.
"""
