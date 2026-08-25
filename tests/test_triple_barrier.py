"""Triple-barrier labeling: barrier-touch logic, ties, gaps, timeouts, costs."""

from __future__ import annotations

import numpy as np

from fxlab.costs.model import CostModel
from fxlab.data.ingest_dukascopy import generate_synthetic_bars
from fxlab.labeling.triple_barrier import apply_triple_barrier, label_one

# index 0 is a pre-entry bar (ignored); entry_idx = 1, tp = 102, sl = 99, entry = 100.
_O = np.array([99.0, 100.0, 100.0, 100.0, 100.0])


def _run(high, low, close, side=1, tp=102.0, sl=99.0, tie=True, open_=None):
    o = _O if open_ is None else np.asarray(open_, float)
    return label_one(
        o, np.asarray(high, float), np.asarray(low, float), np.asarray(close, float),
        entry_idx=1, entry_price=100.0, tp_price=tp, sl_price=sl, max_hold=4,
        side=side, sl_first_on_tie=tie,
    )


def test_long_take_profit_first():
    r = _run(high=[99, 100.5, 102.4, 100, 100], low=[99, 99.5, 99.6, 99.5, 99.5],
             close=[99, 100, 101, 100, 100])
    assert r.outcome == "tp" and r.label == 1 and r.exit_price == 102.0


def test_long_stop_loss_first():
    r = _run(high=[99, 100.5, 100.4, 100, 100], low=[99, 99.5, 98.9, 99.5, 99.5],
             close=[99, 100, 99.5, 100, 100])
    assert r.outcome == "sl" and r.label == 0 and r.exit_price == 99.0


def test_tie_resolves_to_stop_loss_by_default():
    r = _run(high=[99, 100.5, 102.5, 100, 100], low=[99, 99.5, 98.5, 99.5, 99.5],
             close=[99, 100, 100, 100, 100], tie=True)
    assert r.outcome == "sl" and r.label == 0


def test_tie_can_resolve_to_take_profit_when_configured():
    r = _run(high=[99, 100.5, 102.5, 100, 100], low=[99, 99.5, 98.5, 99.5, 99.5],
             close=[99, 100, 100, 100, 100], tie=False)
    assert r.outcome == "tp" and r.label == 1


def test_gap_open_beyond_take_profit_fills_at_open():
    r = _run(high=[99, 100.5, 103.2, 100, 100], low=[99, 99.5, 102.9, 99.5, 99.5],
             close=[99, 100, 103, 100, 100], open_=[99, 100, 103, 100, 100])
    assert r.outcome == "tp" and r.label == 1 and r.exit_price == 103.0  # filled at gap open


def test_gap_open_beyond_stop_loss_fills_at_open():
    r = _run(high=[99, 100.5, 98.2, 100, 100], low=[99, 99.5, 97.5, 99.5, 99.5],
             close=[99, 100, 98, 100, 100], open_=[99, 100, 98, 100, 100])
    assert r.outcome == "sl" and r.label == 0 and r.exit_price == 98.0


def test_timeout_exits_at_last_close():
    r = _run(high=[99, 100.5, 100.5, 100.5, 100.5], low=[99, 99.5, 99.5, 99.5, 99.5],
             close=[99, 100, 100, 100, 100.3])
    assert r.outcome == "timeout" and r.label == 0 and r.exit_price == 100.3


def test_short_take_profit_first():
    # side=-1: tp below (98), sl above (101)
    r = _run(high=[99, 100.5, 100.4, 100, 100], low=[99, 99.5, 97.9, 99.5, 99.5],
             close=[99, 100, 98.5, 100, 100], side=-1, tp=98.0, sl=101.0)
    assert r.outcome == "tp" and r.label == 1 and r.exit_price == 98.0


def test_apply_triple_barrier_shape_latency_and_costs():
    bars = generate_synthetic_bars("EURUSD", "M5", n_bars=500, seed=9)
    sig = np.arange(20, 460, 30)
    side = np.where(np.arange(len(sig)) % 2 == 0, 1, -1)
    cm = CostModel(pip_size=0.0001)
    res = apply_triple_barrier(bars, sig, side, max_hold=24, latency_bars=1, cost_model=cm)

    assert not res.empty
    assert res.index.name == "signal_ts"
    for col in ("entry_ts", "t1", "label", "gross_ret", "net_ret", "outcome"):
        assert col in res.columns
    # entry fills strictly AFTER the signal (latency); label window ends at/after entry.
    assert (res["entry_ts"] > res.index).all()
    assert (res["t1"] >= res["entry_ts"]).all()
    # costs can only hurt: net <= gross for every event.
    assert (res["net_ret"] <= res["gross_ret"] + 1e-12).all()
    assert res["label"].isin([0, 1]).all()
