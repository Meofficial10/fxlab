"""P3.4b — is the USD/JPY-H4 Model D lead a real FVG edge, or trend beta? (train + val only).

STRICTLY EXPLORATORY. The P4 test window (2024+) is NOT touched here. Nothing in this script is
tuned toward the observed result and no threshold is fitted; it asks *why* the one edge-like cell
looks the way it does, and whether the lead survives honest, non-overfit decomposition.

Model D is a *continuation* setup: it enters in the direction of the impulse that created the
fair-value gap, so it is structurally trend-following. The a-priori suspicion is therefore that its
USD/JPY-H4 net edge is **directional beta to the 2021-2023 yen decline** (USD/JPY rose hard), not a
skill in FVG *timing*. Three decompositions test that, on train and validation separately:

  1. SIDE DECOMPOSITION — long vs short expectancy. If the edge is one-sided (all in longs) and the
     pair trended up, that is the signature of trend beta, not a symmetric mechanism edge.
  2. DIRECTIONAL BENCHMARK — "always long" / "always short" through the same engine (one-position
     gated). Measures the ambient per-trade drift each side earns with NO FVG logic at all.
  3. RANDOM-TIMING PLACEBO — N signal sets with Model D's exact long/short counts but at RANDOM
     entry times (fixed seed). Builds a null for expectancy; if Model D sits in the bulk of the
     same-side random null, the FVG *timing* adds nothing beyond the side.

EUR/USD H4 (where Model D fails) is run alongside as a contrast: if EUR/USD longs are ~flat because
the pair did not trend, that *explains* the pair difference without any FVG edge.

Run: ``.venv/Scripts/python.exe scripts/investigate_jpy_d.py``
"""

from __future__ import annotations

import numpy as np

from fxlab.backtest.engine import BacktestConfig, run_backtest
from fxlab.config import load_config
from fxlab.costs.model import CostModel
from fxlab.data.store import load_bars
from fxlab.setups.model_d_fvg_retracement import ModelDFvgRetracement
from fxlab.validation.splits import chronological_split

CELLS = (("USDJPY", "H4"), ("EURUSD", "H4"))  # the lead, and the pair where it fails
SPLITS = ("train", "val")
N_PLACEBO = 1000
SEED = 12345  # fixed and logged; NOT searched over


def _split_bars(cfg, pair, tf, split_name):
    df = load_bars(cfg.data_dir, pair, tf)
    sp = chronological_split(df.index, cfg.split.train_end, cfg.split.val_end)
    table = {"train": sp.train, "val": sp.val}  # test intentionally unreachable
    bars = df.loc[table[split_name]].copy()
    bars.attrs.update(df.attrs)
    return bars


def _exp_net(bars, sig, side, cm, bt) -> tuple[float, int]:
    res = run_backtest(bars, sig, side, cm, bt)
    tr = res.trades
    if tr is None or tr.empty:
        return float("nan"), 0
    return float(tr["net_R"].mean()), len(tr)


def _placebo_null(bars, cm, bt, atr_window, n_sig, n_long, rng) -> np.ndarray:
    """Null expectancies: n_sig random entries with exactly n_long longs, random times."""
    n = len(bars)
    lo, hi = atr_window, n - bt.latency_bars - 2  # valid entry range (warm-up + bounds)
    base_side = np.array([1] * n_long + [-1] * (n_sig - n_long), dtype=int)
    out = np.empty(N_PLACEBO, dtype="float64")
    for i in range(N_PLACEBO):
        pos = rng.integers(lo, hi, size=n_sig)
        sd = base_side.copy()
        rng.shuffle(sd)
        exp, ntk = _exp_net(bars, pos, sd, cm, bt)
        out[i] = exp
    return out


def _subperiod_consistency(cfg, cm, bt) -> None:
    """Split USD/JPY-H4 *train* into 3 chronological chunks; is the lead one window or spread?

    A real structural edge should not live in a single sub-window. This reports per-chunk net
    expectancy AND the long/short split, because the full-train edge sat on the SHORT side.
    """
    bars = _split_bars(cfg, "USDJPY", "H4", "train")
    d = ModelDFvgRetracement()
    sig, side = d.generate(bars)
    res = run_backtest(bars, sig, side, cm, bt)
    tr = res.trades
    # cut trades into 3 equal-count chronological chunks (entry order = signal_ts order)
    tr = tr.sort_values("entry_ts")
    n = len(tr)
    print("\n  --- sub-period consistency, USD/JPY H4 train (3 equal-count chunks) ---")
    for k in range(3):
        lo, hi = k * n // 3, (k + 1) * n // 3
        chunk = tr.iloc[lo:hi]
        lm = chunk["side"].to_numpy() == 1
        net = float(chunk["net_R"].mean())
        nl = float(chunk.loc[lm, "net_R"].mean()) if lm.any() else float("nan")
        ns = float(chunk.loc[~lm, "net_R"].mean()) if (~lm).any() else float("nan")
        t0 = chunk["entry_ts"].iloc[0].date()
        t1 = chunk["entry_ts"].iloc[-1].date()
        print(f"  chunk {k + 1} ({t0}..{t1}, {len(chunk)} tk): net {net:+.4f}  "
              f"[long {nl:+.4f} / short {ns:+.4f}]")


def main() -> None:
    cfg = load_config()
    rng = np.random.default_rng(SEED)

    print("\nP3.4b — USD/JPY-H4 Model D: real FVG edge or trend beta? (train+val; test SEALED)")
    print(f"placebo: {N_PLACEBO} random-timing same-side-count draws, seed={SEED}\n")

    for pair, tf in CELLS:
        cm = CostModel.from_config(cfg.costs, pair)
        bt = BacktestConfig.from_label_config(cfg.label, latency_bars=cm.latency_bars)
        print(f"================  {pair} / {tf}  ================")
        for split_name in SPLITS:
            bars = _split_bars(cfg, pair, tf, split_name)
            n = len(bars)
            drift_pct = 100.0 * (bars["close"].iloc[-1] / bars["close"].iloc[0] - 1.0)

            d = ModelDFvgRetracement()
            sig, side = d.generate(bars)
            n_sig = len(sig)
            n_long_sig = int((side == 1).sum())

            res = run_backtest(bars, sig, side, cm, bt)
            tr = res.trades
            exp_all = float(tr["net_R"].mean())
            g_all = float(tr["gross_R"].mean())
            long_mask = tr["side"].to_numpy() == 1
            n_long_tk = int(long_mask.sum())
            n_short_tk = int((~long_mask).sum())
            exp_long = float(tr.loc[long_mask, "net_R"].mean()) if n_long_tk else float("nan")
            exp_short = float(tr.loc[~long_mask, "net_R"].mean()) if n_short_tk else float("nan")
            g_long = float(tr.loc[long_mask, "gross_R"].mean()) if n_long_tk else float("nan")
            g_short = float(tr.loc[~long_mask, "gross_R"].mean()) if n_short_tk else float("nan")

            # directional benchmarks: always-long / always-short, every bar (one-position gated)
            every = np.arange(0, n - bt.latency_bars - 2)
            long_bench, ntk_l = _exp_net(bars, every, 1, cm, bt)
            short_bench, ntk_s = _exp_net(bars, every, -1, cm, bt)

            # random-timing placebo (matched side counts)
            null = _placebo_null(bars, cm, bt, cfg.label.atr_window, n_sig, n_long_sig, rng)
            null = null[~np.isnan(null)]
            pct = 100.0 * float((null < exp_all).mean())
            z = (exp_all - null.mean()) / null.std(ddof=1) if null.std(ddof=1) > 0 else float("nan")

            print(f"\n  --- {split_name} ({n} bars, close drift {drift_pct:+.1f}%) ---")
            pct_long = 100 * n_long_sig / n_sig
            print(f"  Model D net expR = {exp_all:+.4f} (gross {g_all:+.4f}); "
                  f"{n_long_tk}L / {n_short_tk}S taken, {pct_long:.0f}% long signals")
            print(f"  by side:   long net {exp_long:+.4f} (gross {g_long:+.4f}) | "
                  f"short net {exp_short:+.4f} (gross {g_short:+.4f})")
            print(f"  directional: always-long net {long_bench:+.4f} ({ntk_l} tk) | "
                  f"always-short net {short_bench:+.4f} ({ntk_s} tk)")
            print(f"  placebo null: mean {null.mean():+.4f} sd {null.std(ddof=1):.4f}  "
                  f"-> Model D at {pct:.1f}th pctile, z {z:+.2f}")
        if pair == "USDJPY":
            _subperiod_consistency(cfg, cm, bt)
    print(
        "\nread: if the edge is (a) one-sided long, (b) ~equal to the always-long drift, and "
        "\n(c) inside the same-side random-timing null (pctile ~50, |z|<2), then it is directional "
        "\ntrend beta, NOT an FVG-timing edge -- and it will not generalize to a non-trending pair."
    )


if __name__ == "__main__":
    main()
