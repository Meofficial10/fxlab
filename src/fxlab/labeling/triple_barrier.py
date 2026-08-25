"""Triple-barrier labeling (Phase 1) — the objective form of P(TP before SL).

From an entry filled at ``signal_bar + latency_bars`` (default = next bar open), we set
an upper barrier (TP), a lower barrier (SL), and a vertical barrier (max hold). The
first barrier touched decides the label:

  * ``label = 1`` iff TP is reached before SL within the horizon,
  * ``label = 0`` for SL-first or timeout.

Rules (conservative + leakage-free):
  * **Ties** — if a single bar's range spans both TP and SL, assume **SL first**.
  * **Gaps** — if a bar OPENS beyond a barrier, fill at that open (adverse or favorable).
  * **Timeout** — exit at the last in-horizon bar's close.
  * Outcome is decided on mid prices; costs are applied separately via :class:`CostModel`.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class BarrierResult:
    entry_idx: int
    exit_idx: int
    entry_price: float
    exit_price: float
    label: int          # 1 = TP first, 0 = SL first or timeout
    outcome: str        # "tp" | "sl" | "timeout"
    bars_held: int


def label_one(
    open_: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    entry_idx: int,
    entry_price: float,
    tp_price: float,
    sl_price: float,
    max_hold: int,
    side: int,
    sl_first_on_tie: bool = True,
) -> BarrierResult:
    """Label a single event. ``side`` is +1 (long) or -1 (short)."""
    n = len(high)
    last = min(entry_idx + max_hold - 1, n - 1)

    for i in range(entry_idx, last + 1):
        o, h, lo = open_[i], high[i], low[i]

        if side == 1:
            if i > entry_idx and o >= tp_price:
                return BarrierResult(entry_idx, i, entry_price, o, 1, "tp", i - entry_idx)
            if i > entry_idx and o <= sl_price:
                return BarrierResult(entry_idx, i, entry_price, o, 0, "sl", i - entry_idx)
            hit_tp, hit_sl = h >= tp_price, lo <= sl_price
        else:
            if i > entry_idx and o <= tp_price:
                return BarrierResult(entry_idx, i, entry_price, o, 1, "tp", i - entry_idx)
            if i > entry_idx and o >= sl_price:
                return BarrierResult(entry_idx, i, entry_price, o, 0, "sl", i - entry_idx)
            hit_tp, hit_sl = lo <= tp_price, h >= sl_price

        if hit_tp and hit_sl:
            if sl_first_on_tie:
                return BarrierResult(entry_idx, i, entry_price, sl_price, 0, "sl", i - entry_idx)
            return BarrierResult(entry_idx, i, entry_price, tp_price, 1, "tp", i - entry_idx)
        if hit_tp:
            return BarrierResult(entry_idx, i, entry_price, tp_price, 1, "tp", i - entry_idx)
        if hit_sl:
            return BarrierResult(entry_idx, i, entry_price, sl_price, 0, "sl", i - entry_idx)

    return BarrierResult(entry_idx, last, entry_price, close[last], 0, "timeout", last - entry_idx)


def atr_wilder(bars: pd.DataFrame, window: int = 14) -> pd.Series:
    """Wilder's ATR. Causal: ATR at bar t uses only bars <= t (no look-ahead)."""
    high, low, close = bars["high"], bars["low"], bars["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean()


def apply_triple_barrier(
    bars: pd.DataFrame,
    signal_idx: np.ndarray,
    side: int | np.ndarray,
    tp_mult: float = 2.0,
    sl_mult: float = 1.0,
    max_hold: int = 24,
    atr_window: int = 14,
    latency_bars: int = 1,
    cost_model=None,
    sl_first_on_tie: bool = True,
) -> pd.DataFrame:
    """Label many events. Barriers are ATR(signal-bar)-scaled; entry fills ``latency_bars``
    after the signal. Returns one row per labelled event, indexed by signal timestamp,
    with a ``t1`` column (label-end time) required by purged walk-forward.
    """
    open_ = bars["open"].to_numpy(dtype="float64")
    high = bars["high"].to_numpy(dtype="float64")
    low = bars["low"].to_numpy(dtype="float64")
    close = bars["close"].to_numpy(dtype="float64")
    ts = bars.index
    n = len(bars)
    atr = atr_wilder(bars, atr_window).to_numpy(dtype="float64")

    signal_idx = np.asarray(signal_idx, dtype=int)
    sides = (
        np.full(len(signal_idx), side, dtype=int)
        if np.isscalar(side)
        else np.asarray(side, dtype=int)
    )

    rows = []
    for s, sd in zip(signal_idx, sides, strict=True):
        entry_idx = s + max(1, latency_bars)
        if entry_idx >= n or s < 0 or np.isnan(atr[s]):
            continue
        entry_price = open_[entry_idx]
        dist_tp, dist_sl = tp_mult * atr[s], sl_mult * atr[s]
        if sd == 1:
            tp_price, sl_price = entry_price + dist_tp, entry_price - dist_sl
        else:
            tp_price, sl_price = entry_price - dist_tp, entry_price + dist_sl

        res = label_one(
            open_, high, low, close, entry_idx, entry_price,
            tp_price, sl_price, max_hold, int(sd), sl_first_on_tie,
        )
        gross_ret = sd * (res.exit_price - res.entry_price)
        net_ret = (
            cost_model.net_return_price(gross_ret) if cost_model is not None else gross_ret
        )
        rows.append(
            {
                "signal_ts": ts[s],
                "entry_ts": ts[entry_idx],
                "t1": ts[res.exit_idx],
                "side": int(sd),
                "entry_price": res.entry_price,
                "exit_price": res.exit_price,
                "outcome": res.outcome,
                "label": res.label,
                "bars_held": res.bars_held,
                "atr": atr[s],
                "gross_ret": gross_ret,
                "net_ret": net_ret,
            }
        )

    cols = [
        "signal_ts", "entry_ts", "t1", "side", "entry_price", "exit_price",
        "outcome", "label", "bars_held", "atr", "gross_ret", "net_ret",
    ]
    out = pd.DataFrame(rows, columns=cols)
    if not out.empty:
        out = out.set_index("signal_ts")
    return out
