"""Return-space, volatility-scaled PANEL backtester for multi-asset time-series momentum.

This is deliberately SEPARATE machinery from the single-symbol event engine
(``backtest/engine.py``). That engine is one-position-at-a-time, ATR-triple-barrier and
R-multiple by construction -- the wrong shape for a continuously-held, vol-scaled, many-instrument
portfolio. Model F showed that single-instrument momentum on three correlated FX majors fails P4;
the literature (Moskowitz-Ooi-Pedersen 2012; Hurst-Ooi-Pedersen) says the robust, tradeable form of
time-series momentum needs (a) a broad panel of low-correlation instruments, (b) volatility-scaled
sizing, and (c) portfolio-level accounting. This module provides exactly (a)-(c), in RETURN space.

Why return space (not the pip/point cost model): the existing cost layer is FX-specific
(``pips x pip_size`` + per-lot commission) and silently wrong for an index at 5000 or gold at 2000.
Time-series momentum is a return-space strategy and the literature prices costs in basis points of
turnover, so this engine works in returns and charges ``cost = bps x |delta weight|`` per instrument
(x a stress multiplier). That both fixes the units problem and is the correct portfolio cost model.

LEAKAGE DOCTRINE (as in every other fxlab mechanism -- future-invariance is unit-tested):
    Every weight applied to the return of day ``d`` is decided at a rebalance date STRICTLY
    BEFORE ``d``, from data available at that rebalance only. At rebalance date ``R_k`` we read
    each instrument's trailing-return sign and trailing volatility using bars ``<= R_k``; the
    resulting weight vector earns from the NEXT calendar day onward, until the next rebalance --
    so appending future bars can never change a past weight or a past portfolio return.

Sizing (ex-ante volatility targeting, diagonal / zero-correlation assumption -- a deliberate,
transparent simplification, NOT a fitted covariance):
    Each active instrument ``i`` gets weight ``w_i = side_i * k / sigma_i`` where ``sigma_i`` is
    its ex-ante annualized volatility and ``k = target_ann_vol / sqrt(N_active)``. Under a
    zero-correlation assumption the book's ex-ante annualized vol is then
    ``k * sqrt(N_active) = target_ann_vol`` -- i.e. an equal risk budget per instrument and a
    constant ex-ante portfolio volatility target. Real instruments are positively correlated
    within an asset class, so realized vol runs a little above target; that is a known, documented
    property of diagonal vol-targeting, not an error. Weights are capped at ``+/- weight_cap`` so a
    single low-volatility instrument cannot dominate the book.

Ragged panel: instruments live on slightly different trading calendars and start at different dates.
An instrument contributes to a rebalance only once it has enough native history for BOTH a
``lookback``-bar momentum sign and a ``vol_window`` volatility estimate; otherwise its weight
is 0 (it has not entered the panel yet). Between rebalances the weight vector is held fixed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

TRADING_DAYS = 252  # annualization factor for daily bars (all instruments here are daily)


@dataclass(frozen=True)
class PanelConfig:
    """Frozen TSMOM portfolio knobs. The pre-registered HEADLINE is the defaults below."""

    lookback: int = 252        # trailing bars for the momentum sign (~12 months) -- canonical MOP
    vol_window: int = 60       # trailing bars for the ex-ante volatility estimate (~3 months)
    rebalance_days: int = 21   # rebalance cadence in trading days (~monthly)
    target_ann_vol: float = 0.10   # constant ex-ante annualized portfolio volatility target (10%)
    weight_cap: float = 4.0    # max |weight| per instrument (leverage cap on any single bet)

    def __post_init__(self) -> None:
        if self.lookback < 1 or self.vol_window < 2 or self.rebalance_days < 1:
            raise ValueError("lookback>=1, vol_window>=2, rebalance_days>=1 required")
        if self.target_ann_vol <= 0 or self.weight_cap <= 0:
            raise ValueError("target_ann_vol and weight_cap must be positive")


@dataclass
class PanelResult:
    """Time-indexed portfolio return streams + bookkeeping (all net of costs where noted)."""

    gross_ret: pd.Series          # daily portfolio return, gross of costs
    net_ret: pd.Series            # daily portfolio return, net of turnover costs
    equity: pd.Series             # compounded net equity curve (starts near 1.0 on first held day)
    weights: pd.DataFrame         # held weight per instrument per day (post-rebalance, ffilled)
    rebalance_dates: list[pd.Timestamp]
    turnover_per_rebalance: list[float]   # sum_i |delta w_i| at each rebalance (one-way)
    n_active_per_rebalance: list[int]
    instruments: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- causal signals


def momentum_sign(close: np.ndarray, lookback: int) -> np.ndarray:
    """Sign of the trailing ``lookback``-bar return at each bar; NaN during warm-up.

    ``mom[t] = close[t] - close[t-lookback]`` reads only bars ``<= t`` -> strictly causal and
    future-invariant (identical rule to Model F). Returns +1/-1/0 (0 = flat), NaN for warm-up bars.
    """
    close = np.asarray(close, dtype="float64")
    n = len(close)
    out = np.full(n, np.nan, dtype="float64")
    if n <= lookback:
        return out
    mom = close[lookback:] - close[: n - lookback]
    out[lookback:] = np.sign(mom)
    return out


def ex_ante_volatility(close: np.ndarray, vol_window: int, ann: int = TRADING_DAYS) -> np.ndarray:
    """Trailing annualized volatility of daily returns at each bar; NaN during warm-up.

    Uses the standard deviation of the trailing ``vol_window`` daily simple returns, all ``<= t``
    -> strictly causal. Annualized by ``sqrt(ann)``. NaN until a full window is available.
    """
    close = np.asarray(close, dtype="float64")
    n = len(close)
    out = np.full(n, np.nan, dtype="float64")
    if n < vol_window + 1:
        return out
    ret = np.full(n, np.nan, dtype="float64")
    ret[1:] = close[1:] / close[:-1] - 1.0
    # rolling std (sample, ddof=1) over the trailing vol_window returns, ending at each bar
    s = pd.Series(ret)
    rolling = s.rolling(vol_window).std(ddof=1).to_numpy()
    out[:] = rolling * np.sqrt(ann)
    return out


# --------------------------------------------------------------------------- sizing


def target_weights(
    sides: np.ndarray, vols: np.ndarray, target_ann_vol: float, weight_cap: float
) -> np.ndarray:
    """Vol-targeted weight vector for one rebalance (diagonal / zero-correlation assumption).

    ``w_i = side_i * (target_ann_vol / sqrt(N_active)) / sigma_i``, capped at ``+/- weight_cap``.
    An instrument is *active* iff it has a defined non-zero side and a positive, finite volatility.
    Inactive instruments get weight 0 (they have not entered the panel, or momentum is flat).
    """
    sides = np.asarray(sides, dtype="float64")
    vols = np.asarray(vols, dtype="float64")
    active = np.isfinite(sides) & (sides != 0.0) & np.isfinite(vols) & (vols > 0.0)
    w = np.zeros(len(sides), dtype="float64")
    n_active = int(active.sum())
    if n_active == 0:
        return w
    k = target_ann_vol / np.sqrt(n_active)
    w[active] = sides[active] * k / vols[active]
    return np.clip(w, -weight_cap, weight_cap)


# --------------------------------------------------------------------------- backtest


def _instrument_return_series(close: pd.Series) -> pd.Series:
    """Native daily simple returns of one instrument (first bar -> 0)."""
    ret = close.astype("float64").pct_change()
    ret.iloc[0] = 0.0
    return ret


def run_panel_backtest(
    panel: dict[str, pd.DataFrame],
    cfg: PanelConfig,
    cost_bps: dict[str, float],
    stress_factor: float = 1.0,
) -> PanelResult:
    """Backtest a vol-scaled TSMOM portfolio over a ragged daily panel.

    ``panel``: ``{instrument: bars}`` where bars is a canonical D1 frame (``close`` used).
    ``cost_bps``: one-way transaction cost in BASIS POINTS per instrument, charged on ``|delta w|``.
    ``stress_factor``: multiplies all costs (1.0 = normal, 1.5 = the +50% stress run).

    Returns time-indexed gross/net return streams, the held-weight matrix, and turnover bookkeeping.
    All weights obey the leakage doctrine (decided strictly before the day they earn on).
    """
    instruments = [s for s in panel if panel[s] is not None and not panel[s].empty]
    instruments.sort()
    if not instruments:
        raise ValueError("empty panel")

    # Common calendar = sorted union of all instruments' bar dates (ragged panel).
    calendar = pd.DatetimeIndex(sorted(set().union(*[panel[s].index for s in instruments])))

    # Per-instrument native returns reindexed onto the common calendar (missing days -> 0 return),
    # and causal sign / vol series looked up as-of each rebalance date.
    ret_df = pd.DataFrame(index=calendar, dtype="float64")
    sign_series: dict[str, pd.Series] = {}
    vol_series: dict[str, pd.Series] = {}
    for s in instruments:
        close = panel[s]["close"].astype("float64")
        ret_df[s] = _instrument_return_series(close).reindex(calendar).fillna(0.0)
        sign_series[s] = pd.Series(momentum_sign(close.to_numpy(), cfg.lookback), index=close.index)
        vol_series[s] = pd.Series(
            ex_ante_volatility(close.to_numpy(), cfg.vol_window), index=close.index
        )

    # Rebalance dates: every ``rebalance_days``-th calendar date. Early ones may have no active
    # instrument (all still warming up) -> a flat (all-zero) book until the panel fills in.
    reb_positions = list(range(0, len(calendar), cfg.rebalance_days))
    rebalance_dates = [calendar[p] for p in reb_positions]

    # Build the held-weight matrix: a new vector takes effect the day AFTER its rebalance date,
    # and is held (ffilled) until the next rebalance's vector takes effect.
    target_rows = pd.DataFrame(np.nan, index=calendar, columns=instruments, dtype="float64")
    turnover_per_reb: list[float] = []
    n_active_per_reb: list[int] = []
    prev_w = np.zeros(len(instruments), dtype="float64")
    cost_row = pd.Series(0.0, index=calendar, dtype="float64")
    bps_vec = np.array([cost_bps.get(s, np.nan) for s in instruments], dtype="float64")
    if not np.isfinite(bps_vec).all():
        missing = [s for s, b in zip(instruments, bps_vec, strict=True) if not np.isfinite(b)]
        raise ValueError(f"no cost_bps entry for: {missing}")

    for p in reb_positions:
        r_date = calendar[p]
        sides = np.array([sign_series[s].asof(r_date) for s in instruments], dtype="float64")
        vols = np.array([vol_series[s].asof(r_date) for s in instruments], dtype="float64")
        w = target_weights(sides, vols, cfg.target_ann_vol, cfg.weight_cap)

        turnover = float(np.abs(w - prev_w).sum())
        turnover_per_reb.append(turnover)
        n_active_per_reb.append(int((w != 0.0).sum()))

        # cost (fraction) charged on the day the new weights take effect (the day after rebalance)
        cost = float((bps_vec * 1e-4 * np.abs(w - prev_w)).sum()) * stress_factor
        if p + 1 < len(calendar):
            target_rows.iloc[p + 1] = w
            cost_row.iloc[p + 1] += cost
        prev_w = w

    held = target_rows.ffill().fillna(0.0)

    gross_ret = (held * ret_df).sum(axis=1)
    net_ret = gross_ret - cost_row

    # Metrics/equity start on the first day the book actually holds a position (skip the flat
    # warm-up head so it does not dilute the return statistics).
    held_any = (held != 0.0).any(axis=1)
    if held_any.any():
        start = held_any.idxmax()
        gross_ret = gross_ret.loc[start:]
        net_ret = net_ret.loc[start:]
        held = held.loc[start:]
    equity = (1.0 + net_ret).cumprod()

    return PanelResult(
        gross_ret=gross_ret,
        net_ret=net_ret,
        equity=equity,
        weights=held,
        rebalance_dates=rebalance_dates,
        turnover_per_rebalance=turnover_per_reb,
        n_active_per_rebalance=n_active_per_reb,
        instruments=instruments,
    )
