"""Event-driven, CLOSED-CANDLE-ONLY backtest engine (Phase 2).

This is the single most correctness-critical component in the platform, so it is
deliberately small and its exit logic is *shared* with the labeler rather than
re-derived:

  * A setup emits a signal at the CLOSE of bar ``s`` (decided from bars ``<= s`` only).
  * The entry fills at the OPEN of bar ``s + latency_bars`` (default: the next bar).
  * The exit is resolved by :func:`fxlab.labeling.triple_barrier.label_one` — the exact
    same intrabar rules used for labeling (SL-first on a tie, gap-at-open fills, timeout
    at the last in-horizon close). Because the engine calls ``label_one``, a taken
    trade's exit is byte-identical to its triple-barrier label by construction.

The engine holds **at most one position at a time** (the honest P2 baseline): a signal
whose entry bar falls on or before the current trade's exit bar is skipped. Nothing here
ever reads a bar to the right of the one being processed, so the whole run is
future-invariant for every trade that both opens and closes inside the data window.

Returns are reported sizing-agnostically in **R multiples** (net return ÷ stop distance)
and in **pips**. Account-currency sizing is deferred to the risk engine (P6).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..costs.model import CostModel
from ..data.schema import OHLCV
from ..labeling.triple_barrier import atr_wilder, label_one


@dataclass(frozen=True)
class BacktestConfig:
    tp_atr_mult: float = 2.0
    sl_atr_mult: float = 1.0
    max_hold_bars: int = 24
    atr_window: int = 14
    latency_bars: int = 1
    sl_first_on_tie: bool = True

    @classmethod
    def from_label_config(cls, lc, latency_bars: int = 1) -> BacktestConfig:
        return cls(
            tp_atr_mult=lc.tp_atr_mult,
            sl_atr_mult=lc.sl_atr_mult,
            max_hold_bars=lc.max_hold_bars,
            atr_window=lc.atr_window,
            latency_bars=latency_bars,
        )


TRADE_COLUMNS: list[str] = [
    "signal_ts", "entry_ts", "exit_ts", "side",
    "entry_mid", "exit_mid", "entry_fill", "exit_fill",
    "outcome", "label", "bars_held", "atr",
    "risk_price", "gross_ret_price", "net_ret_price",
    "gross_R", "net_R", "gross_pips", "net_pips",
]


@dataclass
class BacktestResult:
    trades: pd.DataFrame            # one row per TAKEN trade, indexed by signal_ts
    n_signals: int                 # signals the setup produced
    n_taken: int                   # signals actually traded (rest skipped: position busy / bounds)
    symbol: str | None
    timeframe: str | None
    pip_size: float
    config: BacktestConfig
    stressed: bool = False
    meta: dict = field(default_factory=dict)

    @property
    def skipped_busy(self) -> int:
        return self.n_signals - self.n_taken


def run_backtest(
    bars: pd.DataFrame,
    signal_idx: np.ndarray,
    side: np.ndarray | int,
    cost_model: CostModel,
    config: BacktestConfig | None = None,
) -> BacktestResult:
    """Run the closed-candle event loop over ``bars`` for the given signals.

    ``signal_idx`` are integer positions into ``bars`` (bar CLOSE at which the setup
    fired). ``side`` is +1/-1 per signal (or a scalar applied to all).
    """
    cfg = config or BacktestConfig()
    missing = [c for c in OHLCV if c not in bars.columns]
    if missing:
        raise ValueError(f"bars missing required columns: {missing}")

    open_ = bars["open"].to_numpy(dtype="float64")
    high = bars["high"].to_numpy(dtype="float64")
    low = bars["low"].to_numpy(dtype="float64")
    close = bars["close"].to_numpy(dtype="float64")
    ts = bars.index
    n = len(bars)
    atr = atr_wilder(bars, cfg.atr_window).to_numpy(dtype="float64")

    signal_idx = np.asarray(signal_idx, dtype=int)
    order = np.argsort(signal_idx, kind="stable")
    signal_idx = signal_idx[order]
    if np.isscalar(side):
        sides = np.full(len(signal_idx), int(side), dtype=int)
    else:
        sides = np.asarray(side, dtype=int)[order]

    pip_size = cost_model.pip_size
    rows: list[dict] = []
    last_exit_idx = -1  # bar index at which the most recent trade closed

    for s, sd in zip(signal_idx, sides, strict=True):
        entry_idx = s + max(1, cfg.latency_bars)
        # closed-candle + bounds + warm-up ATR guards
        if s < 0 or entry_idx >= n or np.isnan(atr[s]) or atr[s] <= 0.0:
            continue
        # one position at a time: must be flat on the bar we would enter
        if entry_idx <= last_exit_idx:
            continue

        entry_mid = open_[entry_idx]
        dist_tp, dist_sl = cfg.tp_atr_mult * atr[s], cfg.sl_atr_mult * atr[s]
        if sd == 1:
            tp_price, sl_price = entry_mid + dist_tp, entry_mid - dist_sl
        else:
            tp_price, sl_price = entry_mid - dist_tp, entry_mid + dist_sl

        res = label_one(
            open_, high, low, close, entry_idx, entry_mid,
            tp_price, sl_price, cfg.max_hold_bars, int(sd), cfg.sl_first_on_tie,
        )

        gross_ret = sd * (res.exit_price - res.entry_price)
        net_ret = cost_model.net_return_price(gross_ret)
        risk_price = dist_sl  # 1R = the stop distance
        rows.append(
            {
                "signal_ts": ts[s],
                "entry_ts": ts[entry_idx],
                "exit_ts": ts[res.exit_idx],
                "side": int(sd),
                "entry_mid": entry_mid,
                "exit_mid": res.exit_price,
                "entry_fill": cost_model.entry_fill(entry_mid, int(sd)),
                "exit_fill": cost_model.exit_fill(res.exit_price, int(sd)),
                "outcome": res.outcome,
                "label": res.label,
                "bars_held": res.bars_held,
                "atr": atr[s],
                "risk_price": risk_price,
                "gross_ret_price": gross_ret,
                "net_ret_price": net_ret,
                "gross_R": gross_ret / risk_price,
                "net_R": net_ret / risk_price,
                "gross_pips": gross_ret / pip_size,
                "net_pips": net_ret / pip_size,
            }
        )
        last_exit_idx = res.exit_idx

    trades = pd.DataFrame(rows, columns=TRADE_COLUMNS)
    if not trades.empty:
        trades = trades.set_index("signal_ts")

    return BacktestResult(
        trades=trades,
        n_signals=int(len(signal_idx)),
        n_taken=int(len(trades)),
        symbol=bars.attrs.get("symbol"),
        timeframe=bars.attrs.get("timeframe"),
        pip_size=pip_size,
        config=cfg,
        stressed=False,
    )
