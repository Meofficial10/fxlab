"""P4-candidate mechanism #6 -- Model F, time-series momentum / trend-following (train + val only).

This is the second mechanism deliberately *outside* the SMC family (Models A-D) and the first at a
DAILY / multi-week horizon. It is chosen for a documented STRUCTURAL prior (time-series momentum is
the most-replicated cross-asset premium; MOP 2012, Hurst-Ooi-Pedersen), NOT because the last
mechanism failed. It is tested with the identical discipline and held to the identical P4 gate; the
previous results are not used to justify, seed, or force it. The TEST window (2024+) stays SEALED.

FROZEN HYPOTHESIS (UNPROVEN -- measured, never tuned toward):
    The sign of a pair's trailing `lookback`-bar return predicts the sign of its next move, so
    entering in the direction of that trailing-return sign carries positive expectancy net of costs
    at the daily horizon. Structural reason it might survive costs where the intraday session
    breakout (Model E) did not: on D1 the ATR-scaled 1R stop is ~10x larger than intraday, so the
    fixed per-trade spread+commission is a small fraction of R.

The pre-registered HEADLINE is exactly one cell: **D1, lookback = 126 (~6 months)** -- a canonical
TSMOM horizon fixed a priori for horizon-fit and sample adequacy, NOT chosen on results. Declared up
front purely as robustness context (NOT a search for the best): lookbacks 63 and 252, and the H4
timeframe (a shorter economic horizon). Model B is the no-edge baseline. Every cell is reported;
only the frozen headline is judged against P4.

What is measured, on train (in-sample) and val (out-of-sample), for every cell:
  * gross AND net expectancy per trade (R), at normal AND +50% stressed costs;
  * in-sample t of gross R vs 0 (uncorrected -- a single-cell |t|~2 is the noise floor);
  * profit factor (gross) and per-trade cost drag (R).
Plus a purged, embargoed WALK-FORWARD of the headline config per pair/TF: since no parameter is fit,
this is a sequential out-of-sample block evaluation (train+val timeline cut into contiguous blocks;
per-block net expectancy + t reported). Leakage/future-invariance is pinned by the Model F unit
tests; costs/stress come from CostModel/.stress. D1 bars come from ``scripts/build_daily_bars.py``.

Run: ``.venv/Scripts/python.exe scripts/measure_momentum.py``
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from fxlab.backtest.engine import BacktestConfig, run_backtest
from fxlab.backtest.metrics import compute_metrics
from fxlab.config import load_config
from fxlab.costs.model import CostModel
from fxlab.data.schema import timeframe_to_timedelta
from fxlab.data.store import load_bars
from fxlab.experiment.log import hash_bars, log_experiment
from fxlab.setups.model_b_trend_pullback import ModelBTrendPullback
from fxlab.setups.model_f_momentum import ModelFMomentum
from fxlab.validation.splits import chronological_split
from fxlab.validation.walkforward import PurgedWalkForward

PAIRS = ("EURUSD", "GBPUSD", "USDJPY")
TIMEFRAMES = ("D1", "H4")   # D1 = headline horizon; H4 = shorter-horizon robustness context
SPLITS = ("train", "val")   # the TEST window stays SEALED until P4 -- never selected here
WF_SPLITS = 6               # -> 5 sequential OOS test blocks for the walk-forward
HEADLINE = "F mom126"       # the one pre-registered cell-family (D1, lookback 126) judged vs P4
LOOKBACKS = ((63, "F mom63"), (126, HEADLINE), (252, "F mom252"))


def _mk(lb: int):
    """Factory for a Model F at a fixed lookback (avoids late-binding in the config list)."""
    return lambda: ModelFMomentum(lookback=lb)


def _f_configs():
    """model_b baseline (reference) + the three Model F lookbacks. Only HEADLINE is judged."""
    return [("model_b baseline", ModelBTrendPullback), *[(lbl, _mk(lb)) for lb, lbl in LOOKBACKS]]


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


def _available_tfs(cfg, pair) -> list[str]:
    """The configured timeframes actually ingested for a pair (Model F needs daily bars)."""
    out = []
    for tf in TIMEFRAMES:
        try:
            load_bars(cfg.data_dir, pair, tf)
        except FileNotFoundError:
            continue
        out.append(tf)
    return out


def _trainval_bars(cfg, pair, tf):
    """Train+val bars concatenated in time order for the walk-forward. TEST (> val_end) excluded."""
    df = load_bars(cfg.data_dir, pair, tf)
    sp = chronological_split(df.index, cfg.split.train_end, cfg.split.val_end)
    idx = sp.train.append(sp.val)  # disjoint and already ordered (train < val)
    bars = df.loc[idx].copy()
    bars.attrs.update(df.attrs)
    return bars


def _measure_pair(cfg, registry, pair) -> list[tuple]:
    rows: list[tuple] = []
    configs = _f_configs()
    tfs = _available_tfs(cfg, pair)
    for split_name in SPLITS:
        for tf in tfs:
            bars = _split_bars(cfg, pair, tf, split_name)
            data_hash = hash_bars(bars)
            cm = CostModel.from_config(cfg.costs, pair)
            cm_s = cm.stress(cfg.costs.stress_factor)
            bt = BacktestConfig.from_label_config(cfg.label, latency_bars=cm.latency_bars)

            for label, make in configs:
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
        f"{'split':<6}{'tf':<4}{'config':<18}{'trades':>7}{'expR_gr':>9}{'t_gr':>7}"
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
            f"{split_name:<6}{tf:<4}{label:<18}{n:>7}{eg:>+9.4f}{tg:>+7.2f}"
            f"{en:>+10.4f}{ens:>+9.4f}{pf:>7.3f}{cd:>8.4f}"
        )


def _walk_forward(cfg, pair) -> None:
    """Purged/embargoed sequential OOS block evaluation of the headline config (no params fit)."""
    print(f"\n--- {pair} walk-forward (headline '{HEADLINE}', purged blocks, no params fit) ---")
    make = dict(_f_configs())[HEADLINE]
    for tf in _available_tfs(cfg, pair):
        bars = _trainval_bars(cfg, pair, tf)
        cm = CostModel.from_config(cfg.costs, pair)
        cm_s = cm.stress(cfg.costs.stress_factor)
        bt = BacktestConfig.from_label_config(cfg.label, latency_bars=cm.latency_bars)
        sig, side = make().generate(bars)
        tr = run_backtest(bars, sig, side, cm, bt).trades
        tr_s = run_backtest(bars, sig, side, cm_s, bt).trades  # same trade index (costs != gating)
        if tr is None or tr.empty:
            print(f"  {tf}: no trades")
            continue

        # t1 = event start (signal_ts, the index) -> label end (exit_ts): the purge key.
        t1 = pd.Series(tr["exit_ts"].to_numpy(), index=tr.index)
        embargo = timeframe_to_timedelta(tf) * cfg.split.embargo_bars
        wf = PurgedWalkForward(n_splits=WF_SPLITS, embargo=embargo)

        span = f"{bars.index.min().date()}..{bars.index.max().date()}"
        print(f"\n  [{tf}]  {len(tr)} trades over {span}  (embargo {cfg.split.embargo_bars} bars)")
        print(f"  {'block':<7}{'from':<12}{'to':<12}{'n':>6}{'net_R':>10}{'t':>7}{'net_str':>10}")
        n_pos = 0
        all_net = []
        all_net_s = []
        blocks = list(wf.split(t1))
        for i, (_, test_times) in enumerate(blocks, start=1):
            chunk = tr.loc[test_times]
            chunk_s = tr_s.loc[test_times]
            net = chunk["net_R"].to_numpy(dtype="float64")
            net_s = chunk_s["net_R"].to_numpy(dtype="float64")
            all_net.append(net)
            all_net_s.append(net_s)
            n_pos += int(net.mean() > 0)
            print(f"  {i:<7}{str(test_times.min().date()):<12}{str(test_times.max().date()):<12}"
                  f"{len(chunk):>6}{net.mean():>+10.4f}{_t_stat(net):>+7.2f}{net_s.mean():>+10.4f}")
        cat = np.concatenate(all_net) if all_net else np.array([])
        cat_s = np.concatenate(all_net_s) if all_net_s else np.array([])
        if len(cat):
            print(f"  {'ALL':<7}{'':<12}{'':<12}{len(cat):>6}{cat.mean():>+10.4f}"
                  f"{_t_stat(cat):>+7.2f}{cat_s.mean():>+10.4f}   "
                  f"[{n_pos}/{len(blocks)} OOS blocks net>0]")


def main() -> None:
    cfg = load_config()
    registry = Path(cfg.experiments_dir) / "registry.jsonl"

    print("\nP4-candidate mechanism #6 — Model F time-series momentum (NON-SMC, daily horizon)")
    print("frozen hypothesis: trailing-return sign predicts next move; headline = D1 lookback 126.")
    print("train+val only; TEST sealed; nothing tuned toward a positive; prior results not used.\n")

    missing_cells = []
    for pair in PAIRS:
        avail = []
        try:
            avail = _available_tfs(cfg, pair)
        except FileNotFoundError:
            pass
        missing_cells += [f"{pair}/{tf}" for tf in TIMEFRAMES if tf not in avail]
        if not avail:
            continue
        rows = _measure_pair(cfg, registry, pair)
        _print_pair(pair, rows)

    print("\n\n########  WALK-FORWARD (sequential OOS blocks; no parameter is fit)  ########")
    for pair in PAIRS:
        if not _available_tfs(cfg, pair):
            continue
        _walk_forward(cfg, pair)

    if missing_cells:
        print(f"\n[not ingested — cell skipped]: {', '.join(missing_cells)}")
    print(
        "\nexpR_gr/net = mean gross/net R per trade; t_gr = in-sample t of gross R vs 0; "
        "\nnet_str = net R per trade at +50% cost stress; cost_R = per-trade cost drag (R). "
        "\nval = out-of-sample; walk-forward = contiguous OOS blocks (no fit). TEST stays SEALED. "
        "\nP4 needs + net expectancy, SAME side, across >=2 pairs AND >=2 periods, stress-stable. "
        "\nJudged on the HEADLINE only (D1 lookback 126); other cells/TFs are context, not a pick."
    )


if __name__ == "__main__":
    main()
