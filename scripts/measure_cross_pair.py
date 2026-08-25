"""P3.4 cross-pair robustness (train + validation — the P4 test window stays SEALED).

The P4 gate asks for an edge robust across **>=2 pairs**. On EUR/USD the whole plan setup
universe (Models A-D) failed net of costs and out of sample. This driver re-runs the *same*
pipeline the CLI uses on the robustness pairs (GBP/USD, USD/JPY) alongside EUR/USD, for the core
mechanisms only, and prints one comparison table per pair.

The question is narrow and honest: does any of the sweep reversal (A), its one in-sample lift
(A +structure), the breakout-failure reversal (C), or the FVG-retracement continuation (D) carry
net edge on a *different* pair that it lacked on EUR/USD? This tests the robustness of an edge not
yet found; the most likely outcome is that it confirms the negative. Either way the numbers are
whatever they are -- gross AND net are reported, at normal and +50% stressed costs, and no knob is
tuned toward any target. USD/JPY's pip size (0.01) is handled by the per-pair cost config, and all
expectancies are in R (ATR units), so the columns are directly comparable across pairs.

Run: ``.venv/Scripts/python.exe scripts/measure_cross_pair.py``
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from fxlab.backtest.engine import BacktestConfig, run_backtest
from fxlab.backtest.metrics import compute_metrics
from fxlab.config import load_config
from fxlab.costs.model import CostModel
from fxlab.data.store import load_bars
from fxlab.experiment.log import hash_bars, log_experiment
from fxlab.setups.model_a_sweep_reversal import ModelASweepReversal
from fxlab.setups.model_b_trend_pullback import ModelBTrendPullback
from fxlab.setups.model_c_breakout_failure import ModelCBreakoutFailure
from fxlab.setups.model_d_fvg_retracement import ModelDFvgRetracement
from fxlab.validation.splits import chronological_split

PAIRS = ("EURUSD", "GBPUSD", "USDJPY")
TIMEFRAMES = ("H1", "H4")
SPLITS = ("train", "val")  # the TEST window stays sealed until P4 -- never selected here

# The core mechanisms, one line each: the trend-pullback baseline, the sweep reversal and its one
# in-sample lift, and the two independent mechanisms. No per-pair tuning -- identical configs on
# every pair so the comparison is apples-to-apples.
CONFIGS: list[tuple[str, callable]] = [
    ("model_b baseline", lambda: ModelBTrendPullback()),
    ("A base (sweep)", lambda: ModelASweepReversal()),
    ("A +structure", lambda: ModelASweepReversal(align_structure=True)),
    ("C base (breakout-fail)", lambda: ModelCBreakoutFailure()),
    ("D base (fvg-retrace)", lambda: ModelDFvgRetracement()),
]


def _t_stat(x: np.ndarray) -> float:
    """One-sample t vs 0 for per-trade returns (in-sample, no multiple-testing correction)."""
    n = len(x)
    if n < 2:
        return float("nan")
    sd = x.std(ddof=1)
    if sd == 0:
        return float("nan")
    return float(x.mean() / (sd / np.sqrt(n)))


def _split_bars(cfg, pair, tf, split_name):
    """Return train or val bars. TEST is intentionally unreachable here (sealed until P4)."""
    df = load_bars(cfg.data_dir, pair, tf)
    sp = chronological_split(df.index, cfg.split.train_end, cfg.split.val_end)
    table = {"train": sp.train, "val": sp.val}
    if split_name not in table:
        raise ValueError(f"this driver only measures {sorted(table)}; test stays sealed")
    bars = df.loc[table[split_name]].copy()
    bars.attrs.update(df.attrs)
    return bars


def _measure_pair(cfg, registry, pair) -> list[tuple]:
    rows: list[tuple] = []
    for split_name in SPLITS:
        for tf in TIMEFRAMES:
            bars = _split_bars(cfg, pair, tf, split_name)
            data_hash = hash_bars(bars)
            cm = CostModel.from_config(cfg.costs, pair)
            cm_s = cm.stress(cfg.costs.stress_factor)
            bt = BacktestConfig.from_label_config(cfg.label, latency_bars=cm.latency_bars)

            for label, make in CONFIGS:
                strat = make()
                sig, side = strat.generate(bars)

                res = run_backtest(bars, sig, side, cm, bt)
                m = compute_metrics(res.trades)
                res_s = run_backtest(bars, sig, side, cm_s, bt)
                m_s = compute_metrics(res_s.trades)

                gross_R = (
                    res.trades["gross_R"].to_numpy(dtype="float64")
                    if res.trades is not None and not res.trades.empty
                    else np.array([])
                )
                t_gross = _t_stat(gross_R)

                params = {"label": label, **{k: v for k, v in vars(strat).items() if k != "name"}}
                log_experiment(
                    registry, setup=strat.name, symbol=pair, timeframe=tf, split=split_name,
                    params=params, metrics=m.as_dict(), data_hash=data_hash,
                    n_signals=res.n_signals, n_taken=res.n_taken, stressed=False,
                )
                log_experiment(
                    registry, setup=strat.name, symbol=pair, timeframe=tf, split=split_name,
                    params={**params, "stress_factor": cfg.costs.stress_factor},
                    metrics=m_s.as_dict(), data_hash=data_hash,
                    n_signals=res_s.n_signals, n_taken=res_s.n_taken, stressed=True,
                )

                rows.append((
                    split_name, tf, label, m.n_trades, m.expectancy_R_gross, t_gross,
                    m.expectancy_R_net, m_s.expectancy_R_net,
                    m.profit_factor_gross, m.cost_drag_R_per_trade,
                ))
    return rows


def _print_pair(pair: str, rows: list[tuple]) -> None:
    hdr = (
        f"{'split':<6}{'tf':<4}{'config':<24}{'trades':>7}{'expR_gr':>9}{'t_gr':>7}"
        f"{'expR_net':>10}{'net_str':>9}{'PF_gr':>7}{'cost_R':>8}"
    )
    print(f"\n=== {pair} — TRAIN + VAL, in-sample/OOS (test window sealed) ===\n")
    print(hdr)
    print("-" * len(hdr))
    last = None
    for split_name, tf, label, n, eg, tg, en, ens, pf, cd in rows:
        key = (split_name, tf)
        if last and key != last:
            print()
        last = key
        print(
            f"{split_name:<6}{tf:<4}{label:<24}{n:>7}{eg:>+9.4f}{tg:>+7.2f}"
            f"{en:>+10.4f}{ens:>+9.4f}{pf:>7.3f}{cd:>8.4f}"
        )


def main() -> None:
    cfg = load_config()
    registry = Path(cfg.experiments_dir) / "registry.jsonl"

    print("\nP3.4 cross-pair robustness — core mechanisms, EURUSD vs GBPUSD vs USDJPY")
    missing = []
    for pair in PAIRS:
        try:
            rows = _measure_pair(cfg, registry, pair)
        except FileNotFoundError:
            missing.append(pair)
            continue
        _print_pair(pair, rows)

    if missing:
        print(f"\n[skipped — no ingested data]: {', '.join(missing)}")
    print(
        "\nexpR_gr/net = mean gross/net R per trade; t_gr = in-sample t of gross R vs 0; "
        "\nnet_str = net R per trade at +50% cost stress; cost_R = per-trade cost drag (R). "
        "\nval = out-of-sample robustness check; TEST stays sealed until the P4 gate. "
        "\nAn edge worth P4 must be positive net of costs on the SAME side across pairs+periods."
    )


if __name__ == "__main__":
    main()
