"""Transaction-cost model: costs never help, and grow with volatility and stress."""

from __future__ import annotations

import numpy as np

from fxlab.config import CostConfig
from fxlab.costs.model import CostModel


def _cm() -> CostModel:
    return CostModel(pip_size=0.0001)  # spread 0.6, commission 7, slip 0.2 + 0.10*vol


def test_net_return_never_exceeds_gross():
    cm = _cm()
    for g in (-0.0020, 0.0, 0.0005, 0.0030):
        assert cm.net_return_price(g) <= g


def test_costs_are_monotonic_in_volatility():
    cm = _cm()
    vols = np.linspace(0, 10, 25)
    slip = [cm.slippage_price(v) for v in vols]
    assert all(b >= a for a, b in zip(slip, slip[1:], strict=False))  # non-decreasing
    net = [cm.net_return_price(0.0030, v) for v in vols]
    assert all(b <= a for a, b in zip(net, net[1:], strict=False))    # more vol -> never better net


def test_stress_increases_round_turn_cost():
    cm = _cm()
    assert cm.stress(1.5).round_turn_cost_price(0) > cm.round_turn_cost_price(0)


def test_commission_scales_linearly_with_lots():
    cm = _cm()
    assert cm.commission_cost(2.0) == 2.0 * cm.commission_cost(1.0)
    assert cm.commission_cost(1.0) == 7.0


def test_fills_are_adverse_to_the_trade_direction():
    cm = _cm()
    # long: buy above mid on entry, sell below mid on exit
    assert cm.entry_fill(100.0, side=1) > 100.0
    assert cm.exit_fill(100.0, side=1) < 100.0
    # short: sell below mid on entry, buy above mid on exit
    assert cm.entry_fill(100.0, side=-1) < 100.0
    assert cm.exit_fill(100.0, side=-1) > 100.0


def test_net_equals_gross_minus_round_turn():
    cm = _cm()
    g = 0.0025
    assert abs(cm.net_return_price(g, 3.0) - (g - cm.round_turn_cost_price(3.0))) < 1e-15


def test_from_config_resolves_pip_size_per_symbol():
    cfg = CostConfig()
    assert CostModel.from_config(cfg, "USDJPY").pip_size == 0.01
    assert CostModel.from_config(cfg, "EURUSD").pip_size == 0.0001
