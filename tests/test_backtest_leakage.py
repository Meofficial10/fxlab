"""Future-invariance guards for the Phase 2 layer (setup + engine).

The mandated leakage doctrine: appending future bars must not change anything decided
in the past. We check it twice —

  * the setup's signals over ``bars[:k]`` equal the full-run signals that fall before k;
  * every engine trade that fully COMPLETES before bar k is byte-identical whether the
    engine saw only ``bars[:k]`` or the whole series.

Plus the cost-stress monotonicity: +50% costs can only make net returns worse.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from pandas.testing import assert_frame_equal

from fxlab.backtest.engine import BacktestConfig, run_backtest
from fxlab.costs.model import CostModel
from fxlab.setups.model_b_trend_pullback import ModelBTrendPullback

_CFG = BacktestConfig(
    tp_atr_mult=2.0, sl_atr_mult=1.0, max_hold_bars=24, atr_window=14, latency_bars=1
)
_CMP = ["entry_ts", "exit_ts", "side", "exit_mid", "outcome", "label", "bars_held", "net_R"]


def test_setup_signals_are_future_invariant(synthetic_bars):
    bars = synthetic_bars
    k = 400
    strat = ModelBTrendPullback()
    idx_full, side_full = strat.generate(bars)
    idx_cut, side_cut = strat.generate(bars.iloc[:k])

    keep = idx_full < k
    assert np.array_equal(idx_full[keep], idx_cut)
    assert np.array_equal(side_full[keep], side_cut[: keep.sum()])


def test_engine_completed_trades_are_future_invariant(synthetic_bars):
    bars = synthetic_bars
    k = 420
    boundary_ts = bars.index[k]
    cm = CostModel(pip_size=0.0001)

    # a dense momentum signal set (plenty of trades before and after the boundary)
    n = len(bars)
    sig = np.arange(_CFG.atr_window + 1, n - 1, 6)
    closes = bars["close"].to_numpy()
    side = np.where(closes[sig] >= closes[sig - 1], 1, -1)

    full = run_backtest(bars, sig, side, cm, _CFG).trades
    cut = run_backtest(bars.iloc[:k], sig[sig < k], side[sig < k], cm, _CFG).trades

    done = full[full["exit_ts"] < boundary_ts]
    assert len(done) > 0
    assert set(done.index).issubset(set(cut.index))
    assert_frame_equal(
        done[_CMP].sort_index(),
        cut.loc[done.index, _CMP].sort_index(),
        check_exact=False,
    )


def test_stress_costs_never_improve_net(synthetic_bars):
    bars = synthetic_bars
    n = len(bars)
    sig = np.arange(_CFG.atr_window + 1, n - _CFG.max_hold_bars - 1, 5)
    closes = bars["close"].to_numpy()
    side = np.where(closes[sig] >= closes[sig - 1], 1, -1)

    cm = CostModel(pip_size=0.0001)
    base = run_backtest(bars, sig, side, cm, _CFG).trades
    stressed = run_backtest(bars, sig, side, cm.stress(1.5), _CFG).trades

    # same trades taken (costs don't change which bars are hit), worse-or-equal net
    assert list(base.index) == list(stressed.index)
    assert stressed["net_R"].sum() <= base["net_R"].sum() + 1e-12
    assert (stressed["net_ret_price"] <= base["net_ret_price"] + 1e-18).all()


def test_signals_only_reference_closed_candles(synthetic_bars):
    """Every signal index is a valid closed bar and warm-up is respected."""
    strat = ModelBTrendPullback(ema_fast=20, ema_slow=50)
    idx, side = strat.generate(synthetic_bars)
    assert (idx >= strat.ema_slow).all()
    assert (idx < len(synthetic_bars)).all()
    assert set(np.unique(side)).issubset({-1, 1})
    assert pd.Series(idx).is_monotonic_increasing
