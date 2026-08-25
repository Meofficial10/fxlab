"""Model A (sweep reversal) — signal/detector agreement, causality, confirmation filters.

The base setup fires on every liquidity sweep; the optional filters (displacement, FVG,
structure alignment, premium/discount) must each *narrow* that set, compose as an
intersection, respect their direction semantics, and stay strictly causal.
"""

from __future__ import annotations

import numpy as np
import pytest

from fxlab.setups.model_a_sweep_reversal import ModelASweepReversal
from fxlab.smc.displacement import displacement
from fxlab.smc.fvg import fair_value_gaps
from fxlab.smc.liquidity import liquidity_sweeps
from fxlab.smc.premium_discount import premium_discount
from fxlab.structure.market_structure import market_structure

# Every filter flag, exercised singly and in a couple of combinations.
_FILTER_FLAGS = ["align_pd", "require_displacement", "require_fvg", "align_structure"]
_FLAG_SETS = (
    {},
    {"align_pd": True},
    {"require_displacement": True},
    {"require_fvg": True},
    {"align_structure": True},
    {"require_displacement": True, "align_structure": True},
    {"require_displacement": True, "require_fvg": True, "align_structure": True, "align_pd": True},
)


def test_signals_match_sweep_detector(synthetic_bars):
    strat = ModelASweepReversal()
    idx, side = strat.generate(synthetic_bars)
    sweep = liquidity_sweeps(synthetic_bars)["sweep_dir"].to_numpy()
    # Every fired bar is a sweep bar, and side is exactly the sweep direction.
    assert (sweep[idx] != 0).all()
    assert np.array_equal(side, sweep[idx])
    # Completeness: the base setup fires on *every* sweep, and there is something to filter.
    assert np.array_equal(idx, np.flatnonzero(sweep != 0))
    assert len(idx) > 0


@pytest.mark.parametrize("kwargs", [{"left": 0}, {"right": 0}, {"atr_window": 0}, {"body_mult": 0}])
def test_config_validation(kwargs):
    with pytest.raises(ValueError):
        ModelASweepReversal(**kwargs)


@pytest.mark.parametrize("flags", _FLAG_SETS)
def test_signals_future_invariant(synthetic_bars, flags):
    strat = ModelASweepReversal(**flags)
    idx_full, side_full = strat.generate(synthetic_bars)
    for k in (120, 300, 520):
        idx_t, side_t = strat.generate(synthetic_bars.iloc[:k])
        keep = idx_full < k
        assert np.array_equal(idx_t, idx_full[keep])
        assert np.array_equal(side_t, side_full[keep])


@pytest.mark.parametrize("flag", _FILTER_FLAGS)
def test_each_filter_is_a_subset_of_base(synthetic_bars, flag):
    base_idx, _ = ModelASweepReversal().generate(synthetic_bars)
    idx, side = ModelASweepReversal(**{flag: True}).generate(synthetic_bars)
    # A filter can only remove signals, never add or relabel them.
    assert set(idx.tolist()) <= set(base_idx.tolist())
    sweep = liquidity_sweeps(synthetic_bars)["sweep_dir"].to_numpy()
    assert np.array_equal(side, sweep[idx])


def test_combined_filters_are_the_intersection(synthetic_bars):
    a = set(ModelASweepReversal(require_displacement=True).generate(synthetic_bars)[0].tolist())
    b = set(ModelASweepReversal(align_structure=True).generate(synthetic_bars)[0].tolist())
    both, _ = ModelASweepReversal(
        require_displacement=True, align_structure=True
    ).generate(synthetic_bars)
    assert set(both.tolist()) == (a & b)


def test_displacement_filter_direction(synthetic_bars):
    idx, side = ModelASweepReversal(require_displacement=True).generate(synthetic_bars)
    disp = displacement(synthetic_bars)["disp_dir"].to_numpy()
    for i, s in zip(idx.tolist(), side.tolist(), strict=True):
        assert disp[i] == s  # displacement is in the reversal (sweep) direction


def test_fvg_filter_direction(synthetic_bars):
    idx, side = ModelASweepReversal(require_fvg=True).generate(synthetic_bars)
    fvg = fair_value_gaps(synthetic_bars)["fvg_dir"].to_numpy()
    for i, s in zip(idx.tolist(), side.tolist(), strict=True):
        assert fvg[i] == s


def test_structure_filter_direction(synthetic_bars):
    idx, side = ModelASweepReversal(align_structure=True).generate(synthetic_bars)
    trend = market_structure(synthetic_bars)["trend_state"].to_numpy()
    for i, s in zip(idx.tolist(), side.tolist(), strict=True):
        assert trend[i] == ("bull" if s == 1 else "bear")


def test_align_pd_is_a_subset_in_the_right_zones(synthetic_bars):
    base_idx, _ = ModelASweepReversal(align_pd=False).generate(synthetic_bars)
    idx, side = ModelASweepReversal(align_pd=True).generate(synthetic_bars)
    assert set(idx.tolist()) <= set(base_idx.tolist())
    # Longs only in discount, shorts only in premium.
    zone = premium_discount(synthetic_bars)["pd_zone"].to_numpy()
    for i, s in zip(idx.tolist(), side.tolist(), strict=True):
        assert zone[i] == ("discount" if s == 1 else "premium")
