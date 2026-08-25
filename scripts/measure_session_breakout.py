"""P4-candidate mechanism #5 — Model E, session opening-range breakout (train + val only).

This is the first mechanism deliberately *outside* the SMC family (Models A-D). It is tested with
the identical discipline and held to the identical P4 gate; the previous SMC results are NOT used to
justify, seed, or force it. The TEST window (2024+) stays SEALED — never selected here.

FROZEN HYPOTHESIS (UNPROVEN — measured, never tuned toward):
    Intraday FX has a time-of-day volatility structure. When a major session opens (canonically
    London, 07:00-16:00 UTC), participation/volatility rise and the level at which the session's
    first range breaks tends to mark the direction flow commits to. So a breakout of the session's
    OPENING RANGE, taken in the breakout direction, *may* carry positive expectancy net of costs.

The pre-registered HEADLINE test of that hypothesis is exactly one cell-family: **London, or_bars
= 1**. Two further configs are declared up front purely as robustness context (NOT a search for the
best): a wider 2-bar opening range, and the New York open (same mechanism, a different major
session). Model B is the no-edge baseline. Every cell is reported (nothing is selected post
hoc); only the frozen headline is judged against P4.

What is measured, on train (in-sample) and val (out-of-sample), for every cell:
  * gross AND net expectancy per trade (R), at normal AND +50% stressed costs (realistic spread +
    commission + slippage from the per-pair cost config; USD/JPY pip=0.01 handled there);
  * in-sample t of gross R vs 0 (uncorrected — a single-cell |t|~2 is the demonstrated noise floor);
  * profit factor (gross) and per-trade cost drag (R).
Plus a purged, embargoed WALK-FORWARD for the headline config per pair/TF: since no parameter is
fit, this is a sequential out-of-sample block evaluation (train+val timeline cut into contiguous
blocks; per-block net expectancy + t reported). Leakage/future-invariance is already pinned by the
14 Model E unit tests; costs/stress come from CostModel/.stress.

Run: ``.venv/Scripts/python.exe scripts/measure_session_breakout.py``
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
from fxlab.setups.model_e_session_breakout import ModelESessionBreakout
from fxlab.validation.splits import chronological_split
from fxlab.validation.walkforward import PurgedWalkForward

PAIRS = ("EURUSD", "GBPUSD", "USDJPY")
TIMEFRAMES = ("H1", "M15")  # H4 excluded: a session is ~2 H4 bars, so there is no opening range
SPLITS = ("train", "val")   # the TEST window stays SEALED until P4 -- never selected here
WF_SPLITS = 6               # -> 5 sequential OOS test blocks for the walk-forward
HEADLINE = "E London or1"   # the one pre-registered cell-family judged against P4


def _e_configs(cfg):
    """Model E variants keyed to config session windows. London-or1 is the frozen headline;
    the other two are pre-declared robustness context, not a tuned search."""
    sess = {s.name: (s.start_hour, s.end_hour) for s in cfg.data.sessions}
    lon = sess.get("London", (7, 16))
    nyk = sess.get("NewYork", (12, 21))

    def _e(start, end, or_bars):
        return lambda: ModelESessionBreakout(start_hour=start, end_hour=end, or_bars=or_bars)

    return [
        (HEADLINE, _e(lon[0], lon[1], 1)),
        ("E London or2", _e(lon[0], lon[1], 2)),
        ("E NewYork or1", _e(nyk[0], nyk[1], 1)),
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


def _available_tfs(cfg, pair) -> list[str]:
    """The configured timeframes actually ingested for a pair (Model E needs intraday bars)."""
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
    configs = [("model_b baseline", ModelBTrendPullback), *_e_configs(cfg)]
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
    make = dict(_e_configs(cfg))[HEADLINE]
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

    print("\nP4-candidate mechanism #5 — Model E session opening-range breakout (NON-SMC)")
    print("frozen hypothesis: OR breakout carries + net expectancy; headline = London or_bars=1.")
    print("train+val only; TEST sealed; nothing tuned toward a positive; SMC results not used.\n")

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
        "\nJudged on the HEADLINE only (London or1); other cells are context, not a post-hoc pick."
    )


if __name__ == "__main__":
    main()
