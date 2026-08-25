"""Panel TSMOM backtester — leakage guards + sizing/accounting correctness.

Mirrors the doctrine of ``tests/test_backtest_leakage.py`` for the new portfolio machinery:
  * causal signals (momentum sign AND ex-ante vol) are future-invariant;
  * vol-targeting gives an inverse-vol weight and hits the portfolio-vol target when uncorrelated;
  * +50% cost stress can only make the net return worse (turnover-cost monotonicity);
  * a tiny hand-built 2-instrument panel reproduces the exact return/accounting identity.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from fxlab.backtest.panel import (
    PanelConfig,
    ex_ante_volatility,
    momentum_sign,
    run_panel_backtest,
    target_weights,
)
from fxlab.backtest.portfolio_metrics import compute_portfolio_metrics
from fxlab.data.schema import ensure_bars


def _d1(closes, symbol="TEST", start="2019-01-01") -> pd.DataFrame:
    closes = np.asarray(closes, dtype="float64")
    idx = pd.date_range(start, periods=len(closes), freq="1D", tz="UTC")
    df = pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes}, index=idx
    )
    return ensure_bars(df, symbol, "D1")


# --------------------------------------------------------------------------- causal signals


def test_momentum_sign_is_future_invariant():
    rng = np.random.default_rng(1)
    close = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.01, size=400)))
    full = momentum_sign(close, lookback=20)
    for k in (100, 250, 399):
        cut = momentum_sign(close[:k], lookback=20)
        assert np.array_equal(cut, full[:k], equal_nan=True)


def test_ex_ante_vol_is_future_invariant():
    rng = np.random.default_rng(2)
    close = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.01, size=400)))
    full = ex_ante_volatility(close, vol_window=30)
    for k in (100, 250, 399):
        cut = ex_ante_volatility(close[:k], vol_window=30)
        assert np.array_equal(cut, full[:k], equal_nan=True)


def test_panel_weights_are_future_invariant():
    # A held weight decided at an early rebalance must not change when future bars are appended.
    rng = np.random.default_rng(3)
    a = 100.0 * np.exp(np.cumsum(rng.normal(0.0003, 0.01, size=500)))
    b = 100.0 * np.exp(np.cumsum(rng.normal(-0.0002, 0.012, size=500)))
    cfg = PanelConfig(lookback=60, vol_window=30, rebalance_days=10, weight_cap=10.0)
    bps = {"A": 1.0, "B": 1.0}

    full = run_panel_backtest({"A": _d1(a, "A"), "B": _d1(b, "B")}, cfg, bps)
    cutoff = full.weights.index[300]
    part = run_panel_backtest(
        {"A": _d1(a[:320], "A"), "B": _d1(b[:320], "B")}, cfg, bps
    )
    shared = full.weights.index[full.weights.index < cutoff].intersection(part.weights.index)
    assert len(shared) > 50  # non-vacuous
    pd.testing.assert_frame_equal(full.weights.loc[shared], part.weights.loc[shared])


# --------------------------------------------------------------------------- sizing


def test_double_vol_gets_half_weight():
    w = target_weights(np.array([1.0, 1.0]), np.array([0.10, 0.20]), target_ann_vol=0.1,
                       weight_cap=100.0)
    assert np.isclose(w[0], 2.0 * w[1])
    assert w[0] > 0 and w[1] > 0


def test_weight_cap_binds():
    w = target_weights(np.array([1.0]), np.array([0.001]), target_ann_vol=0.1, weight_cap=3.0)
    assert np.isclose(w[0], 3.0)


def test_inactive_instruments_get_zero_weight():
    w = target_weights(
        np.array([0.0, 1.0, np.nan, 1.0]),
        np.array([0.1, np.nan, 0.1, 0.0]),
        target_ann_vol=0.1, weight_cap=10.0,
    )
    assert np.array_equal(w, np.zeros(4))  # flat side / nan vol / zero vol -> not invested


def test_portfolio_hits_vol_target_on_uncorrelated_data():
    # Independent instruments -> diagonal vol-target is ~exact (realized ann vol ~ target).
    rng = np.random.default_rng(7)
    n_inst, n_days, target = 8, 6000, 0.10
    daily_sigma = np.linspace(0.005, 0.02, n_inst)
    ann_vols = daily_sigma * np.sqrt(252)
    sides = np.ones(n_inst)
    w = target_weights(sides, ann_vols, target_ann_vol=target, weight_cap=1e9)  # no cap
    rets = rng.normal(0.0, daily_sigma, size=(n_days, n_inst))
    port = rets @ w
    realized_ann_vol = port.std(ddof=1) * np.sqrt(252)
    assert abs(realized_ann_vol / target - 1.0) < 0.15


# --------------------------------------------------------------------------- accounting


def test_zero_cost_net_equals_gross_and_accounting_identity():
    rng = np.random.default_rng(11)
    a = 100.0 * np.exp(np.cumsum(rng.normal(0.001, 0.01, size=200)))
    b = 50.0 * np.exp(np.cumsum(rng.normal(-0.001, 0.015, size=200)))
    panel = {"A": _d1(a, "A"), "B": _d1(b, "B")}
    cfg = PanelConfig(lookback=20, vol_window=10, rebalance_days=5, weight_cap=10.0)

    res = run_panel_backtest(panel, cfg, {"A": 0.0, "B": 0.0})
    # zero costs -> net is exactly gross
    pd.testing.assert_series_equal(res.net_ret, res.gross_ret, check_names=False)
    # equity is the compounded net stream
    assert np.allclose(res.equity.to_numpy(), (1.0 + res.net_ret).cumprod().to_numpy())
    # gross return each day is exactly held-weight . instrument-return
    ret_a = _d1(a, "A")["close"].pct_change().reindex(res.weights.index).fillna(0.0)
    ret_b = _d1(b, "B")["close"].pct_change().reindex(res.weights.index).fillna(0.0)
    recomputed = res.weights["A"] * ret_a + res.weights["B"] * ret_b
    assert np.allclose(recomputed.to_numpy(), res.gross_ret.to_numpy())


def test_stress_costs_never_improve_net():
    rng = np.random.default_rng(13)
    a = 100.0 * np.exp(np.cumsum(rng.normal(0.001, 0.01, size=300)))
    b = 50.0 * np.exp(np.cumsum(rng.normal(-0.001, 0.02, size=300)))
    panel = {"A": _d1(a, "A"), "B": _d1(b, "B")}
    cfg = PanelConfig(lookback=30, vol_window=15, rebalance_days=5, weight_cap=10.0)
    bps = {"A": 2.0, "B": 3.0}

    base = run_panel_backtest(panel, cfg, bps, stress_factor=1.0)
    stress = run_panel_backtest(panel, cfg, bps, stress_factor=1.5)

    # same positions (costs don't change weights), gross identical, net worse-or-equal everywhere
    pd.testing.assert_frame_equal(base.weights, stress.weights)
    assert np.allclose(base.gross_ret.to_numpy(), stress.gross_ret.to_numpy())
    assert (stress.net_ret.to_numpy() <= base.net_ret.to_numpy() + 1e-15).all()
    assert stress.net_ret.sum() < base.net_ret.sum()  # some turnover exists -> strictly worse


def test_known_signs_on_trending_instruments():
    # A rises (mom +), B falls (mom -): after warm-up the book is long A, short B.
    a = np.linspace(1.0, 2.0, 60)      # strictly increasing
    b = np.linspace(2.0, 1.0, 60)      # strictly decreasing
    panel = {"A": _d1(a, "A"), "B": _d1(b, "B")}
    cfg = PanelConfig(lookback=5, vol_window=5, rebalance_days=5, weight_cap=10.0)
    res = run_panel_backtest(panel, cfg, {"A": 1.0, "B": 1.0})
    # once both are held, weight signs must be +A / -B
    held = res.weights[(res.weights["A"] != 0) & (res.weights["B"] != 0)]
    assert len(held) > 0
    assert (held["A"] > 0).all()
    assert (held["B"] < 0).all()


def test_portfolio_metrics_are_finite_and_consistent():
    rng = np.random.default_rng(17)
    a = 100.0 * np.exp(np.cumsum(rng.normal(0.001, 0.01, size=400)))
    b = 50.0 * np.exp(np.cumsum(rng.normal(0.0005, 0.02, size=400)))
    panel = {"A": _d1(a, "A"), "B": _d1(b, "B")}
    cfg = PanelConfig(lookback=40, vol_window=20, rebalance_days=10, weight_cap=10.0)
    res = run_panel_backtest(panel, cfg, {"A": 1.0, "B": 1.0})
    m = compute_portfolio_metrics(
        res.gross_ret, res.net_ret, res.equity, res.turnover_per_rebalance, cfg.rebalance_days
    )
    assert m.n_days == len(res.net_ret)
    assert 0.0 <= m.max_drawdown_frac <= 1.0
    assert m.cost_drag_ann >= -1e-9  # costs can only reduce net vs gross
    assert np.isfinite(m.ann_vol) and m.ann_vol > 0
