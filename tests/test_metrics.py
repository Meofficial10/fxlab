"""Metric math on hand-built trade logs with known answers."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from fxlab.backtest.metrics import compute_metrics, metrics_by_session


def _trades(net_R):
    net_R = np.asarray(net_R, dtype="float64")
    outcome = np.where(net_R > 0, "tp", np.where(net_R < 0, "sl", "timeout"))
    idx = pd.date_range("2020-01-06 00:00", periods=len(net_R), freq="1h", tz="UTC")
    return pd.DataFrame(
        {
            "entry_ts": idx,
            "net_R": net_R,
            "gross_R": net_R + 0.1,          # fixed 0.1 R cost drag
            "net_pips": net_R * 10.0,
            "gross_pips": (net_R + 0.1) * 10.0,
            "outcome": outcome,
        },
        index=idx,
    )


def test_full_metric_set_matches_known_values():
    m = compute_metrics(_trades([2, -1, 2, -1, -1, 2]))
    assert m.n_trades == 6
    assert m.n_wins == 3 and m.n_losses == 3 and m.n_timeouts == 0
    assert m.win_rate == 0.5
    assert math.isclose(m.expectancy_R_net, 0.5)
    assert math.isclose(m.expectancy_R_gross, 0.6)
    assert math.isclose(m.avg_win_R_net, 2.0)
    assert math.isclose(m.avg_loss_R_net, -1.0)
    assert math.isclose(m.profit_factor_net, 2.0)     # 6 / 3
    assert math.isclose(m.total_R_net, 3.0)
    assert math.isclose(m.max_drawdown_R, 2.0)        # cum [2,1,3,2,1,3], peak 3 -> trough 1
    assert m.longest_win_streak == 1
    assert m.longest_loss_streak == 2
    assert math.isclose(m.cost_drag_R_per_trade, 0.1)


def test_profit_factor_edge_cases():
    assert math.isinf(compute_metrics(_trades([1.0, 2.0])).profit_factor_net)  # no losses
    empty = compute_metrics(pd.DataFrame())
    assert empty.n_trades == 0
    assert math.isnan(empty.profit_factor_net)
    assert math.isnan(empty.expectancy_R_net)


def test_by_session_partitions_all_trades():
    sessions = [
        {"name": "Asia", "start_hour": 0, "end_hour": 8},
        {"name": "London", "start_hour": 8, "end_hour": 16},
    ]
    trades = _trades([2, -1, 2, -1, -1, 2])  # 6 hourly trades from 00:00 UTC
    by = metrics_by_session(trades, sessions)
    assert sum(m.n_trades for m in by.values()) == len(trades)
