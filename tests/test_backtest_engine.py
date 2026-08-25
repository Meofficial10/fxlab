"""The event-driven engine must agree with the labeler and never leak.

The strongest correctness anchor: for every trade the engine TAKES, its exit is
byte-identical to the triple-barrier label for that same signal — because the engine
resolves exits by calling the very same ``label_one``. We also check the one-position
rule, the cost inequality, and the deterministic R bounds of clean barrier hits.
"""

from __future__ import annotations

import numpy as np

from fxlab.backtest.engine import BacktestConfig, run_backtest
from fxlab.backtest.metrics import compute_metrics
from fxlab.costs.model import CostModel
from fxlab.labeling.triple_barrier import apply_triple_barrier

_CFG = BacktestConfig(
    tp_atr_mult=2.0, sl_atr_mult=1.0, max_hold_bars=24, atr_window=14, latency_bars=1
)


def _dense_signals(bars, every=5):
    n = len(bars)
    sig = np.arange(_CFG.atr_window + 1, n - _CFG.max_hold_bars - 1, every)
    closes = bars["close"].to_numpy()
    side = np.where(closes[sig] >= closes[sig - 1], 1, -1)
    return sig, side


def test_engine_exits_match_labeler_for_taken_trades(synthetic_bars):
    bars = synthetic_bars
    sig, side = _dense_signals(bars, every=7)
    cm = CostModel(pip_size=0.0001)

    lab = apply_triple_barrier(
        bars, sig, side, tp_mult=_CFG.tp_atr_mult, sl_mult=_CFG.sl_atr_mult,
        max_hold=_CFG.max_hold_bars, atr_window=_CFG.atr_window,
        latency_bars=_CFG.latency_bars, cost_model=cm,
    )
    res = run_backtest(bars, sig, side, cm, _CFG)
    eng = res.trades

    assert res.n_taken > 0 and res.n_taken <= res.n_signals
    for ts, row in eng.iterrows():
        lr = lab.loc[ts]
        assert row["entry_ts"] == lr["entry_ts"]
        assert row["exit_ts"] == lr["t1"]
        assert row["exit_mid"] == lr["exit_price"]
        assert row["outcome"] == lr["outcome"]
        assert int(row["label"]) == int(lr["label"])
        assert int(row["bars_held"]) == int(lr["bars_held"])
        assert abs(row["net_ret_price"] - lr["net_ret"]) < 1e-15


def test_only_one_position_at_a_time(synthetic_bars):
    bars = synthetic_bars
    sig, side = _dense_signals(bars, every=2)  # force heavy overlap
    res = run_backtest(bars, sig, side, CostModel(pip_size=0.0001), _CFG)
    eng = res.trades.sort_values("entry_ts")
    entries = eng["entry_ts"].to_numpy()
    exits = eng["exit_ts"].to_numpy()
    # each trade opens strictly after the previous one closed
    assert (entries[1:] > exits[:-1]).all()


def test_net_never_exceeds_gross(synthetic_bars):
    bars = synthetic_bars
    sig, side = _dense_signals(bars, every=5)
    res = run_backtest(bars, sig, side, CostModel(pip_size=0.0001), _CFG)
    t = res.trades
    assert (t["net_ret_price"] <= t["gross_ret_price"] + 1e-18).all()
    assert (t["net_R"] <= t["gross_R"] + 1e-12).all()
    assert (t["net_pips"] <= t["gross_pips"] + 1e-9).all()


def test_clean_barrier_hits_have_expected_R_bounds(synthetic_bars):
    bars = synthetic_bars
    sig, side = _dense_signals(bars, every=5)
    res = run_backtest(bars, sig, side, CostModel(pip_size=0.0001), _CFG)
    t = res.trades
    ratio = _CFG.tp_atr_mult / _CFG.sl_atr_mult
    # TP (barrier or favourable gap) -> gross R >= tp/sl ; SL -> gross R <= -1
    tp = t[t["outcome"] == "tp"]
    sl = t[t["outcome"] == "sl"]
    assert (tp["gross_R"] >= ratio - 1e-9).all()
    assert (sl["gross_R"] <= -1.0 + 1e-9).all()


def test_metrics_are_consistent_with_trades(synthetic_bars):
    bars = synthetic_bars
    sig, side = _dense_signals(bars, every=5)
    res = run_backtest(bars, sig, side, CostModel(pip_size=0.0001), _CFG)
    m = compute_metrics(res.trades)
    assert m.n_trades == res.n_taken
    assert m.n_wins + m.n_losses <= m.n_trades
    assert abs(m.expectancy_R_net - res.trades["net_R"].mean()) < 1e-12
    # costs strictly drag expectancy down (spread > 0)
    assert m.expectancy_R_net < m.expectancy_R_gross
