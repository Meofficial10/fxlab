"""Backtest metrics (Phase 2) — the honest full set, reported GROSS and NET.

The P2 gate requires the complete descriptive picture, not a single flattering number:
count, win rate, average win/loss, **expectancy**, profit factor, max drawdown, and
win/loss streaks. Every money-like quantity is shown both gross and net so the cost drag
is explicit. **Win rate is reported but is never the objective** — expectancy net of
costs is (a high hit rate with bad R:R still loses money).

Returns are summarised in R multiples (primary, sizing-agnostic) and pips.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from .portfolio import cumulative_R, max_drawdown


def _profit_factor(returns: np.ndarray) -> float:
    """Sum of wins ÷ |sum of losses|. inf if no losses, nan if no trades."""
    if len(returns) == 0:
        return float("nan")
    gains = returns[returns > 0].sum()
    losses = -returns[returns < 0].sum()
    if losses == 0:
        return float("inf") if gains > 0 else float("nan")
    return float(gains / losses)


def _longest_streak(mask: np.ndarray) -> int:
    """Longest run of True in ``mask``."""
    best = run = 0
    for v in mask:
        run = run + 1 if v else 0
        best = max(best, run)
    return int(best)


@dataclass
class Metrics:
    n_trades: int
    n_wins: int
    n_losses: int
    n_timeouts: int
    win_rate: float
    # R multiples (primary)
    expectancy_R_net: float
    expectancy_R_gross: float
    avg_win_R_net: float
    avg_loss_R_net: float
    # pips
    expectancy_pips_net: float
    expectancy_pips_gross: float
    avg_win_pips_net: float
    avg_loss_pips_net: float
    # aggregate / risk
    profit_factor_net: float
    profit_factor_gross: float
    total_R_net: float
    total_R_gross: float
    max_drawdown_R: float
    longest_win_streak: int
    longest_loss_streak: int
    # cost transparency
    cost_drag_R_per_trade: float   # gross - net, per trade, in R

    def as_dict(self) -> dict:
        return asdict(self)


def compute_metrics(trades: pd.DataFrame) -> Metrics:
    """Compute the full metric set from an engine trade log (may be empty)."""
    if trades is None or trades.empty:
        z = float("nan")
        return Metrics(
            0, 0, 0, 0, z, z, z, z, z, z, z, z, z, z, z, 0.0, 0.0, 0.0, 0, 0, z
        )

    net_R = trades["net_R"].to_numpy(dtype="float64")
    gross_R = trades["gross_R"].to_numpy(dtype="float64")
    net_pips = trades["net_pips"].to_numpy(dtype="float64")
    gross_pips = trades["gross_pips"].to_numpy(dtype="float64")
    outcomes = trades["outcome"].to_numpy()

    wins = net_R > 0
    losses = net_R < 0
    n = len(net_R)

    dd = max_drawdown(cumulative_R(net_R))

    return Metrics(
        n_trades=n,
        n_wins=int(wins.sum()),
        n_losses=int(losses.sum()),
        n_timeouts=int((outcomes == "timeout").sum()),
        win_rate=float(wins.mean()),
        expectancy_R_net=float(net_R.mean()),
        expectancy_R_gross=float(gross_R.mean()),
        avg_win_R_net=float(net_R[wins].mean()) if wins.any() else 0.0,
        avg_loss_R_net=float(net_R[losses].mean()) if losses.any() else 0.0,
        expectancy_pips_net=float(net_pips.mean()),
        expectancy_pips_gross=float(gross_pips.mean()),
        avg_win_pips_net=float(net_pips[wins].mean()) if wins.any() else 0.0,
        avg_loss_pips_net=float(net_pips[losses].mean()) if losses.any() else 0.0,
        profit_factor_net=_profit_factor(net_R),
        profit_factor_gross=_profit_factor(gross_R),
        total_R_net=float(net_R.sum()),
        total_R_gross=float(gross_R.sum()),
        max_drawdown_R=float(dd.max_drawdown),
        longest_win_streak=_longest_streak(wins),
        longest_loss_streak=_longest_streak(losses),
        cost_drag_R_per_trade=float(gross_R.mean() - net_R.mean()),
    )


def metrics_by_session(trades: pd.DataFrame, sessions) -> dict[str, Metrics]:
    """Per-session metrics (regime/session-segmented reporting, per the charter).

    Tags each trade by the session of its ENTRY time. ``sessions`` is the config's
    session-window list; requires :func:`fxlab.data.schema.tag_sessions`.
    """
    from ..data.schema import tag_sessions

    if trades is None or trades.empty:
        return {}
    entry_index = pd.DatetimeIndex(trades["entry_ts"])
    tags = tag_sessions(entry_index, sessions)
    out: dict[str, Metrics] = {}
    for sess in pd.unique(tags):
        subset = trades.loc[np.asarray(tags) == sess]
        out[str(sess)] = compute_metrics(subset)
    return out
