"""Transaction-cost model (Phase 1).

Costs are always modelled; backtests report gross AND net. A ``stress`` copy scales
spread and slippage (the plan's +50% robustness check). Slippage grows with volatility.

Return convention: prices are treated as mid. A round turn pays the full spread plus
slippage on entry and exit; ``net_return_price`` subtracts that from a gross price move.
Commission is an account-currency (USD) charge applied at the PnL stage, not in price.
"""

from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class CostModel:
    pip_size: float
    spread_pips: float = 0.6
    commission_per_lot_roundturn: float = 7.0
    slippage_pips_base: float = 0.2
    slippage_vol_coeff: float = 0.10
    latency_bars: int = 1

    # --- price-space costs ---
    def half_spread_price(self) -> float:
        return 0.5 * self.spread_pips * self.pip_size

    def slippage_price(self, norm_vol: float = 0.0) -> float:
        pips = self.slippage_pips_base + self.slippage_vol_coeff * max(0.0, norm_vol)
        return pips * self.pip_size

    def entry_fill(self, mid: float, side: int, norm_vol: float = 0.0) -> float:
        """Adverse fill: pay half-spread + slippage in the direction of the trade."""
        return mid + side * (self.half_spread_price() + self.slippage_price(norm_vol))

    def exit_fill(self, mid: float, side: int, norm_vol: float = 0.0) -> float:
        return mid - side * (self.half_spread_price() + self.slippage_price(norm_vol))

    def round_turn_cost_price(self, norm_vol: float = 0.0) -> float:
        """Total price given up on a round turn (full spread + slippage both sides)."""
        return self.spread_pips * self.pip_size + 2.0 * self.slippage_price(norm_vol)

    def net_return_price(self, gross_return_price: float, norm_vol: float = 0.0) -> float:
        """Net price move after round-turn costs. Always <= gross (costs never help)."""
        return gross_return_price - self.round_turn_cost_price(norm_vol)

    # --- account-currency costs ---
    def commission_cost(self, lots: float = 1.0) -> float:
        return self.commission_per_lot_roundturn * lots

    # --- robustness ---
    def stress(self, factor: float) -> CostModel:
        return replace(
            self,
            spread_pips=self.spread_pips * factor,
            slippage_pips_base=self.slippage_pips_base * factor,
            slippage_vol_coeff=self.slippage_vol_coeff * factor,
        )

    @classmethod
    def from_config(cls, cost_config, symbol: str) -> CostModel:
        d = cost_config.default
        return cls(
            pip_size=cost_config.pip_size_for(symbol),
            spread_pips=d.spread_pips,
            commission_per_lot_roundturn=d.commission_per_lot_roundturn,
            slippage_pips_base=d.slippage_pips_base,
            slippage_vol_coeff=d.slippage_vol_coeff,
            latency_bars=d.latency_bars,
        )
