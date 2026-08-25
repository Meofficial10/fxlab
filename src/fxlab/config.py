"""Typed, YAML-backed configuration (Phase 1).

Config is code: every knob lives in ``config/*.yaml`` and is validated here so a
run is fully reproducible from its config. Scalar top-level fields may be
overridden by ``FXLAB_``-prefixed environment variables (pydantic-settings).
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# project_root/config  (this file is src/fxlab/config.py)
CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


class SessionWindow(BaseModel):
    name: str
    start_hour: int = Field(ge=0, le=23)
    end_hour: int = Field(ge=0, le=24)


class DataConfig(BaseModel):
    symbols: list[str] = ["EURUSD"]
    base_timeframe: str = "M5"
    timeframes: list[str] = ["M5", "M15", "H1", "H4"]
    source: str = "dukascopy"
    date_range: dict[str, str] = {}
    sessions: list[SessionWindow] = []


class CostDefaults(BaseModel):
    spread_pips: float = 0.6
    commission_per_lot_roundturn: float = 7.0
    slippage_pips_base: float = 0.2
    slippage_vol_coeff: float = 0.10
    latency_bars: int = 1


class CostConfig(BaseModel):
    pip_size: dict[str, float] = {}
    default: CostDefaults = CostDefaults()
    stress_factor: float = 1.5

    def pip_size_for(self, symbol: str) -> float:
        """Pip size for a symbol; JPY pairs default to 0.01, others to 0.0001."""
        if symbol in self.pip_size:
            return self.pip_size[symbol]
        return 0.01 if symbol.upper().endswith("JPY") else 0.0001


class RiskConfig(BaseModel):
    max_risk_per_trade_pct: float = 1.0
    max_daily_loss_pct: float = 3.0
    max_consecutive_losses: int = 5
    max_drawdown_pct: float = 20.0
    max_trades_per_day: int = 2
    starting_equity: float = 10.0
    ruin_threshold_pct: float = 50.0


class SplitConfig(BaseModel):
    train_end: str
    val_end: str
    embargo_bars: int = 10


class LabelConfig(BaseModel):
    max_hold_bars: int = 24
    tp_atr_mult: float = 2.0
    sl_atr_mult: float = 1.0
    atr_window: int = 14


class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FXLAB_", env_nested_delimiter="__", extra="ignore"
    )

    project_name: str = "fxlab"
    data_dir: str = "data"
    experiments_dir: str = "experiments"
    split: SplitConfig
    label: LabelConfig = LabelConfig()
    data: DataConfig = DataConfig()
    costs: CostConfig = CostConfig()
    risk: RiskConfig = RiskConfig()


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_config(config_dir: Path | str | None = None) -> AppConfig:
    """Load and validate the full config tree from ``config/*.yaml``."""
    d = Path(config_dir) if config_dir else CONFIG_DIR
    merged = {
        **_load_yaml(d / "default.yaml"),
        "data": _load_yaml(d / "data.yaml"),
        "costs": _load_yaml(d / "costs.yaml"),
        "risk": _load_yaml(d / "risk.yaml"),
    }
    return AppConfig(**merged)
