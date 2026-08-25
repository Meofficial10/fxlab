"""P3.1 cost-to-edge measurement (train split only — the P4 test window stays SEALED).

Reproducible driver behind the "lift the cost-to-edge ratio" experiment. For each timeframe
and each Model-A filter combination (plus the Model-B baseline) it runs the *same* pipeline
the CLI uses — objective signals -> event-driven backtest -> full metrics -> experiment log —
at normal and +50% stressed costs, and prints one compact comparison table.

The question is narrow and honest: does making the sweep setup more selective (displacement /
FVG / structure confirmation), or moving to a higher timeframe, lift expectancy *net of costs*
toward the P4 gate? And do the *independent* mechanisms — Model D's FVG-retracement continuation
and Model C's breakout-failure (fakeout) reversal — carry net edge the sweep reversal did not? A
filter or a model that helps gross but not net is a finding, not a success. No knob here is tuned
toward any target; the numbers are whatever they are.

Run: ``.venv/Scripts/python.exe scripts/measure_cost_to_edge.py``
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

PAIR = "EURUSD"
TIMEFRAMES = ("H1", "H4")
SPLITS = ("train", "val")  # the TEST window stays sealed until P4 — never selected here

# (label, factory). Base first, then one filter at a time, then combinations.
CONFIGS: list[tuple[str, callable]] = [
    ("model_b baseline", lambda: ModelBTrendPullback()),
    ("A base (every sweep)", lambda: ModelASweepReversal()),
    ("A +displacement", lambda: ModelASweepReversal(require_displacement=True)),
    ("A +structure", lambda: ModelASweepReversal(align_structure=True)),
    ("A +fvg", lambda: ModelASweepReversal(require_fvg=True)),
    (
        "A +disp+struct",
        lambda: ModelASweepReversal(require_displacement=True, align_structure=True),
    ),
    ("A +pd", lambda: ModelASweepReversal(align_pd=True)),
    (
        "A all filters",
        lambda: ModelASweepReversal(
            require_displacement=True, require_fvg=True, align_structure=True, align_pd=True
        ),
    ),
    # Model D — an INDEPENDENT SMC mechanism (FVG-retracement continuation, not a sweep
    # reversal). "D +min_gap" applies a fixed 10-pip gap floor (NOT tuned per timeframe) as the
    # direct analogue of the Model-A selectivity lever: does trading only material imbalances
    # concentrate edge? The question is the same — net of costs, out of sample.
    ("D base (all FVGs)", lambda: ModelDFvgRetracement()),
    ("D +min_gap 10p", lambda: ModelDFvgRetracement(min_gap=0.0010)),
    # Model C — a THIRD independent mechanism (breakout-failure / fakeout reversal). Its natural
    # selectivity lever is max_wait: a shorter reclaim window keeps only the sharpest traps.
    # "C wait 3" is that stricter variant; neither value is tuned toward any target.
    ("C base (wait 10)", lambda: ModelCBreakoutFailure()),
    ("C wait 3 (prompt)", lambda: ModelCBreakoutFailure(max_wait=3)),
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


def _split_bars(cfg, tf, split_name):
    """Return train or val bars. TEST is intentionally unreachable here (sealed until P4)."""
    df = load_bars(cfg.data_dir, PAIR, tf)
    sp = chronological_split(df.index, cfg.split.train_end, cfg.split.val_end)
    table = {"train": sp.train, "val": sp.val}
    if split_name not in table:
        raise ValueError(f"this driver only measures {sorted(table)}; test stays sealed")
    bars = df.loc[table[split_name]].copy()
    bars.attrs.update(df.attrs)
    return bars


def main() -> None:
    cfg = load_config()
    registry = Path(cfg.experiments_dir) / "registry.jsonl"
    rows = []

    for split_name in SPLITS:
        for tf in TIMEFRAMES:
            bars = _split_bars(cfg, tf, split_name)
            data_hash = hash_bars(bars)
            cm = CostModel.from_config(cfg.costs, PAIR)
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
                    registry, setup=strat.name, symbol=PAIR, timeframe=tf, split=split_name,
                    params=params, metrics=m.as_dict(), data_hash=data_hash,
                    n_signals=res.n_signals, n_taken=res.n_taken, stressed=False,
                )
                log_experiment(
                    registry, setup=strat.name, symbol=PAIR, timeframe=tf, split=split_name,
                    params={**params, "stress_factor": cfg.costs.stress_factor},
                    metrics=m_s.as_dict(), data_hash=data_hash,
                    n_signals=res_s.n_signals, n_taken=res_s.n_taken, stressed=True,
                )

                rows.append((
                    split_name, tf, label, m.n_trades, m.expectancy_R_gross, t_gross,
                    m.expectancy_R_net, m_s.expectancy_R_net,
                    m.profit_factor_gross, m.cost_drag_R_per_trade,
                ))

    hdr = (
        f"{'split':<6}{'tf':<4}{'config':<22}{'trades':>7}{'expR_gr':>9}{'t_gr':>7}"
        f"{'expR_net':>10}{'net_str':>9}{'PF_gr':>7}{'cost_R':>8}"
    )
    print("\nP3.1 cost-to-edge — EURUSD, TRAIN + VAL, in-sample/OOS (test window sealed)\n")
    print(hdr)
    print("-" * len(hdr))
    last = None
    for split_name, tf, label, n, eg, tg, en, ens, pf, cd in rows:
        key = (split_name, tf)
        if last and key != last:
            print()
        last = key
        print(
            f"{split_name:<6}{tf:<4}{label:<22}{n:>7}{eg:>+9.4f}{tg:>+7.2f}"
            f"{en:>+10.4f}{ens:>+9.4f}{pf:>7.3f}{cd:>8.4f}"
        )
    print(
        "\nexpR_gr/net = mean gross/net R per trade; t_gr = in-sample t of gross R vs 0; "
        "\nnet_str = net R per trade at +50% cost stress; cost_R = per-trade cost drag (R). "
        "\nval = out-of-sample model-selection check; TEST stays sealed until the P4 gate."
    )


if __name__ == "__main__":
    main()
