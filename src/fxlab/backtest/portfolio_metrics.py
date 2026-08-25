"""Portfolio-level performance metrics for the TSMOM panel backtester.

These are the P4 judgement fields for a *portfolio* -- risk-adjusted, net of costs, NEVER win rate
(charter section 1). A continuously-held, vol-scaled book has no per-trade R multiple, so the
single-symbol ``Metrics`` (expectancy_R, profit_factor, ...) do not apply. Instead we report the
standard portfolio statistics from a time-indexed daily return stream:

  * annualized return (geometric) and annualized volatility,
  * Sharpe (mean/vol, rf = 0) and Sortino (mean/downside-vol),
  * calendar-time max drawdown (fraction of the running peak),
  * annualized turnover (round-trip notional traded per year),
  * gross vs net, so the cost drag is explicit.

Max drawdown reuses ``backtest.portfolio.max_drawdown`` on the compounded equity curve.
Nothing here interprets a result; it only computes honest numbers.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from fxlab.backtest.panel import TRADING_DAYS
from fxlab.backtest.portfolio import max_drawdown


@dataclass
class PortfolioMetrics:
    n_days: int
    ann_return_net: float
    ann_return_gross: float
    ann_vol: float
    sharpe_net: float
    sharpe_gross: float
    sortino_net: float
    max_drawdown_frac: float      # peak-to-trough decline as a fraction of the running peak (>= 0)
    cost_drag_ann: float          # ann_return_gross - ann_return_net (annualized cost bill)
    turnover_ann: float           # average one-way turnover per year (sum_i |delta w_i| annualized)
    pct_days_invested: float

    def as_dict(self) -> dict:
        return {k: (float(v) if isinstance(v, float) else v) for k, v in asdict(self).items()}


def _sharpe(daily: np.ndarray, ann: int) -> float:
    if len(daily) < 2:
        return float("nan")
    sd = daily.std(ddof=1)
    return float("nan") if sd == 0 else float(daily.mean() / sd * np.sqrt(ann))


def _sortino(daily: np.ndarray, ann: int) -> float:
    if len(daily) < 2:
        return float("nan")
    downside = np.minimum(daily, 0.0)
    dd = np.sqrt(np.mean(downside**2))
    return float("nan") if dd == 0 else float(daily.mean() / dd * np.sqrt(ann))


def _ann_return_geom(daily: np.ndarray, ann: int) -> float:
    if len(daily) == 0:
        return float("nan")
    growth = float(np.prod(1.0 + daily))
    if growth <= 0.0:  # wiped out -> report total loss, don't take a root of a negative
        return -1.0
    years = len(daily) / ann
    return float(growth ** (1.0 / years) - 1.0) if years > 0 else float("nan")


def _max_drawdown_frac(equity: np.ndarray) -> float:
    """Fractional max drawdown of a (positive) equity curve, via portfolio.max_drawdown."""
    if len(equity) == 0:
        return float("nan")
    stats = max_drawdown(equity)  # magnitude + the peak preceding the worst trough (curve units)
    return float(stats.max_drawdown / stats.peak_value) if stats.peak_value > 0 else float("nan")


def compute_portfolio_metrics(
    gross_ret: pd.Series,
    net_ret: pd.Series,
    equity: pd.Series,
    turnover_per_rebalance: list[float],
    rebalance_days: int,
    ann: int = TRADING_DAYS,
) -> PortfolioMetrics:
    """Portfolio metrics from daily gross/net return streams + the net equity curve."""
    g = gross_ret.to_numpy(dtype="float64")
    n = net_ret.to_numpy(dtype="float64")
    eq = equity.to_numpy(dtype="float64")

    ann_ret_net = _ann_return_geom(n, ann)
    ann_ret_gross = _ann_return_geom(g, ann)

    # average one-way turnover per rebalance, annualized by rebalances/year
    rebalances_per_year = ann / rebalance_days
    avg_turnover = float(np.mean(turnover_per_rebalance)) if turnover_per_rebalance else 0.0
    turnover_ann = avg_turnover * rebalances_per_year

    invested = float(np.mean(n != 0.0)) if len(n) else 0.0

    return PortfolioMetrics(
        n_days=int(len(n)),
        ann_return_net=ann_ret_net,
        ann_return_gross=ann_ret_gross,
        ann_vol=float(n.std(ddof=1) * np.sqrt(ann)) if len(n) > 1 else float("nan"),
        sharpe_net=_sharpe(n, ann),
        sharpe_gross=_sharpe(g, ann),
        sortino_net=_sortino(n, ann),
        max_drawdown_frac=_max_drawdown_frac(eq),
        cost_drag_ann=(
            ann_ret_gross - ann_ret_net
            if np.isfinite(ann_ret_gross) and np.isfinite(ann_ret_net)
            else float("nan")
        ),
        turnover_ann=turnover_ann,
        pct_days_invested=invested,
    )
