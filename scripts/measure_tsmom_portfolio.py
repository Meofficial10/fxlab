"""P4-candidate mechanism #7 -- multi-asset time-series momentum, PORTFOLIO form (train + val only).

This is a genuinely different build from Models A-F. Those were single-instrument, one-position,
ATR-triple-barrier, R-multiple mechanisms, and all six failed P4. Model F (daily TSMOM on three
USD-correlated FX majors) is the key signpost: it confirmed the *cost* prior of daily momentum but
still failed -- exactly the configuration the literature (Moskowitz-Ooi-Pedersen 2012; Hurst-Ooi-
Pedersen "A Century of Evidence") says will NOT work. The robust, tradeable form of time-series
momentum needs (a) a BROAD panel of low-correlation instruments, (b) VOLATILITY-SCALED sizing, and
(c) PORTFOLIO-level accounting. This driver measures exactly that, in return space, over a real
14-instrument cross-asset panel (7 FX majors + 2 metals + 2 energy + 3 equity indices).

FROZEN HYPOTHESIS (UNPROVEN -- measured, never tuned toward):
    A diversified book that, at each monthly rebalance, holds each instrument long/short by the sign
    of its trailing 12-month return, sized inversely to that instrument's own recent volatility
    (equal ex-ante risk per instrument, constant ex-ante portfolio-vol target), earns a POSITIVE
    Sharpe NET of realistic costs -- robust across sub-periods and asset classes and stable under a
    +50% cost stress. The diversification across many low-correlation return streams -- absent in
    Models A-F -- is the specific structural reason it may succeed where single-instrument momentum
    failed.

PRE-REGISTERED HEADLINE (fixed a priori, the ONLY cell judged vs P4):
    lookback = 252 trading days (~12m, canonical MOP) | vol_window = 60 | rebalance = 21 (monthly)
    | target_ann_vol = 10% | weight_cap = 4.0.
Declared up front purely as robustness CONTEXT (reported, NOT judged, NOT a search for the best):
    a 63-day lookback, and a weekly (rebalance = 5) cadence.

What is measured, on train (in-sample) and val (OUT-of-sample), for the headline and each context:
    net & gross annualized return, annualized vol, Sharpe (net & gross), Sortino, calendar-time max
    drawdown, annualized turnover, and the annualized cost drag -- at NORMAL and +50% STRESSED
    costs.
Plus (headline only): a sequential OOS SUB-PERIOD block evaluation (the train+val timeline cut into
contiguous calendar blocks; nothing is fit, so each block is out-of-sample), and a DROP-ONE-ASSET-
CLASS re-run (val Sharpe with each class removed) to prove the result does not hang on one class.

Costs are charged in RETURN SPACE: a documented per-instrument one-way basis-point cost on turnover
(``cost = bps x |delta weight|``), NOT the FX pip/point model (which is silently wrong off-FX). The
bps table below is a set of DOCUMENTED ASSUMPTIONS calibrated to the existing FX cost config and to
liquid-CFD norms -- never fabricated results; the +50% stress probes sensitivity to them.

The TEST window (2024+) stays SEALED: each instrument is truncated to ``<= val_end`` at load,
so this driver can never read a 2024+ bar. A failed gate is a legitimate finding; no ML
regardless.

Run: ``.venv/Scripts/python.exe scripts/measure_tsmom_portfolio.py``
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pandas as pd

from fxlab.backtest.panel import PanelConfig, run_panel_backtest
from fxlab.backtest.portfolio_metrics import PortfolioMetrics, compute_portfolio_metrics
from fxlab.config import load_config
from fxlab.data.store import load_bars
from fxlab.experiment.log import hash_bars, log_experiment
from fxlab.validation.splits import to_utc_ts

PANEL_STAGE = "panel"

# The real core cross-asset panel built by scripts/build_tsmom_universe.py (all 14 kept).
ASSET_CLASSES: dict[str, list[str]] = {
    "fx": ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "NZDUSD"],
    "metal": ["XAUUSD", "XAGUSD"],
    "energy": ["BRENT", "WTI"],
    "index": ["SPX500", "NAS100", "GER40"],
}
UNIVERSE: list[str] = [s for syms in ASSET_CLASSES.values() for s in syms]

# --------------------------------------------------------------------------------------------------
# Return-space one-way transaction cost per instrument, in BASIS POINTS of notional traded.
# DOCUMENTED ASSUMPTIONS (never fabricated results); the +50% stress run probes sensitivity:
#   * FX majors: the existing FX config is a 0.6-pip spread + $7/lot round-turn commission, which on
#     a major works out to ~0.8-1.0 bp one-way of notional -> 1.0 bp. The commodity-bloc crosses
#     (AUD/CAD/NZD/CHF) trade a little wider -> 1.2-1.5 bp.
#   * Metals: XAU spot ~2 bp one-way; XAG runs wider on a thinner book -> 4 bp.
#   * Energy CFDs (Brent/WTI): ~3 bp one-way.
#   * Equity-index CFDs: deep and cheap -- US indices ~1.5 bp, DAX ~2 bp one-way.
# All well inside the liquid, well-understood cost regime the universe was restricted to.
# --------------------------------------------------------------------------------------------------
COST_BPS: dict[str, float] = {
    "EURUSD": 1.0, "GBPUSD": 1.0, "USDJPY": 1.0,
    "AUDUSD": 1.2, "USDCAD": 1.2, "USDCHF": 1.2, "NZDUSD": 1.5,
    "XAUUSD": 2.0, "XAGUSD": 4.0,
    "BRENT": 3.0, "WTI": 3.0,
    "SPX500": 1.5, "NAS100": 1.5, "GER40": 2.0,
}

HEADLINE_LABEL = "headline"
HEADLINE_CFG = PanelConfig()  # defaults ARE the pre-registered headline (lb252/vw60/reb21/tv0.10)
CONTEXT_CFGS: list[tuple[str, PanelConfig]] = [
    ("ctx:lb63", PanelConfig(lookback=63)),          # 3-month lookback, monthly rebalance
    ("ctx:weekly", PanelConfig(rebalance_days=5)),   # weekly rebalance, 12-month lookback
]

N_BLOCKS = 6                       # sequential OOS sub-period blocks over the train+val timeline
STRESS_FACTOR = 1.5                # +50% cost stress
SUBPERIOD_MIN = N_BLOCKS // 2 + 1  # strict-majority bar for sub-period robustness (>= 4 of 6)


# --------------------------------------------------------------------------------- panel loading


def _asset_class(symbol: str) -> str:
    return next((cls for cls, syms in ASSET_CLASSES.items() if symbol in syms), "?")


def load_panel(cfg, symbols: list[str], upto: pd.Timestamp) -> dict[str, pd.DataFrame]:
    """Load each instrument's panel D1 bars, TRUNCATED to ``<= upto`` (seals the 2024+ test window).

    Missing instruments are skipped and logged (never fabricated); the driver runs on whatever real
    panel data is on disk.
    """
    panel: dict[str, pd.DataFrame] = {}
    missing: list[str] = []
    for s in symbols:
        try:
            bars = load_bars(cfg.data_dir, s, "D1", stage=PANEL_STAGE)
        except FileNotFoundError:
            missing.append(s)
            continue
        bars = bars.loc[:upto]
        if bars.empty:
            missing.append(s)
            continue
        panel[s] = bars
    if missing:
        print(f"[panel] MISSING (skipped, not fabricated): {', '.join(missing)}")
    return panel


def _panel_hash(panel: dict[str, pd.DataFrame]) -> str:
    """Fingerprint of exact panel (per-instrument content hashes combined)."""
    h = hashlib.sha256()
    for s in sorted(panel):
        h.update(s.encode())
        h.update(hash_bars(panel[s]).encode())
    return h.hexdigest()[:16]


# ------------------------------------------------------------------------------------- windowing


def _window_metrics(
    res, lo: pd.Timestamp | None, hi: pd.Timestamp, rebalance_days: int
) -> tuple[PortfolioMetrics | None, int, int]:
    """Portfolio metrics on the [lo, hi] slice (lo=None -> from series start). Window-local equity.

    Returns (metrics, n_rebalances_in_window, total_active_positions_in_window).
    """
    idx = res.net_ret.index
    mask = idx <= hi if lo is None else (idx > lo) & (idx <= hi)
    n = res.net_ret[mask]
    g = res.gross_ret[mask]
    if n.empty:
        return None, 0, 0
    eq = (1.0 + n).cumprod()  # window-local equity so drawdown is measured within the window
    in_win = [
        (t, a)
        for d, t, a in zip(
            res.rebalance_dates, res.turnover_per_rebalance, res.n_active_per_rebalance, strict=True
        )
        if (d <= hi if lo is None else lo < d <= hi)
    ]
    turns = [t for t, _ in in_win]
    m = compute_portfolio_metrics(g, n, eq, turns, rebalance_days)
    return m, len(turns), int(sum(a for _, a in in_win))


def _sub_period_blocks(res, rebalance_days: int) -> tuple[list[dict], int]:
    """Slice train+val stream into N_BLOCKS contiguous blocks; net stats."""
    n = res.net_ret
    g = res.gross_ret
    if len(n) < N_BLOCKS:
        return [], 0
    bounds = np.linspace(0, len(n), N_BLOCKS + 1, dtype=int)
    blocks: list[dict] = []
    positive = 0
    for i in range(N_BLOCKS):
        sl = slice(bounds[i], bounds[i + 1])
        nb, gb = n.iloc[sl], g.iloc[sl]
        if nb.empty:
            continue
        eqb = (1.0 + nb).cumprod()
        mb = compute_portfolio_metrics(gb, nb, eqb, [], rebalance_days)
        positive += int(mb.ann_return_net > 0)
        blocks.append({
            "block": i + 1,
            "from": nb.index.min().date(),
            "to": nb.index.max().date(),
            "n_days": mb.n_days,
            "ann_ret_net": mb.ann_return_net,
            "sharpe_net": mb.sharpe_net,
        })
    return blocks, positive


# ------------------------------------------------------------------------------------- printing


def _fmt_row(split: str, mode: str, m: PortfolioMetrics) -> str:
    return (
        f"{split:<6}{mode:<8}{m.n_days:>7}{m.ann_return_net * 100:>+9.2f}{m.ann_vol * 100:>7.2f}"
        f"{m.sharpe_net:>+8.2f}{m.sharpe_gross:>+8.2f}{m.sortino_net:>+9.2f}"
        f"{m.max_drawdown_frac * 100:>8.1f}{m.turnover_ann:>8.2f}{m.cost_drag_ann * 100:>8.2f}"
        f"{m.pct_days_invested * 100:>7.1f}"
    )


def _table_header() -> str:
    return (
        f"{'split':<6}{'mode':<8}{'days':>7}{'annRet%':>9}{'vol%':>7}{'Shrp':>8}{'Shp_gr':>8}"
        f"{'Sortino':>9}{'maxDD%':>8}{'turn/y':>8}{'cost%':>8}{'inv%':>7}"
    )


# ------------------------------------------------------------------------------------- measurement


def _run_config(panel, pcfg: PanelConfig) -> tuple:
    """Run normal + stressed backtests once over the full <=val_end panel for one config."""
    res = run_panel_backtest(panel, pcfg, COST_BPS, stress_factor=1.0)
    res_s = run_panel_backtest(panel, pcfg, COST_BPS, stress_factor=STRESS_FACTOR)
    return res, res_s


def _measure_and_log(
    cfg, registry, panel, label: str, pcfg: PanelConfig, train_end, val_end, data_hash: str
) -> dict[str, PortfolioMetrics]:
    """Measure train/val x normal/stress for one config, print the table, log every cell."""
    res, res_s = _run_config(panel, pcfg)
    windows = {"train": (None, train_end), "val": (train_end, val_end)}
    params_base = {
        "label": label, "lookback": pcfg.lookback, "vol_window": pcfg.vol_window,
        "rebalance_days": pcfg.rebalance_days, "target_ann_vol": pcfg.target_ann_vol,
        "weight_cap": pcfg.weight_cap, "instruments": panel_instruments(panel),
        "cost_bps": {s: COST_BPS[s] for s in panel},
    }

    print(f"\n=== config '{label}'  (lb={pcfg.lookback}, vw={pcfg.vol_window}, "
          f"reb={pcfg.rebalance_days}, tgt_vol={pcfg.target_ann_vol:.0%}, "
          f"cap={pcfg.weight_cap}) ===")
    print(_table_header())
    print("-" * len(_table_header()))

    out: dict[str, PortfolioMetrics] = {}
    for split, (lo, hi) in windows.items():
        m, n_reb, n_taken = _window_metrics(res, lo, hi, pcfg.rebalance_days)
        m_s, _, _ = _window_metrics(res_s, lo, hi, pcfg.rebalance_days)
        if m is None or m_s is None:
            print(f"{split:<6}(no data in window)")
            continue
        print(_fmt_row(split, "normal", m))
        print(_fmt_row(split, "stress", m_s))
        out[f"{split}:normal"] = m
        out[f"{split}:stress"] = m_s
        log_experiment(
            registry, setup="tsmom_panel", symbol="PANEL_core", timeframe="D1", split=split,
            params=params_base, metrics=m.as_dict(), data_hash=data_hash,
            n_signals=n_reb, n_taken=n_taken, stressed=False, phase="P4",
        )
        log_experiment(
            registry, setup="tsmom_panel", symbol="PANEL_core", timeframe="D1", split=split,
            params={**params_base, "stress_factor": STRESS_FACTOR}, metrics=m_s.as_dict(),
            data_hash=data_hash, n_signals=n_reb, n_taken=n_taken, stressed=True, phase="P4",
        )
    return out


def panel_instruments(panel) -> list[str]:
    return sorted(panel)


def _drop_one_class(cfg, registry, panel, train_end, val_end, data_hash: str) -> dict[str, float]:
    """Headline val net Sharpe with each asset class removed -- guards single-class dependence."""
    print("\n--- drop-one-asset-class (headline, VAL, net of costs) ---")
    print(f"{'removed':<10}{'kept':>5}{'classes':>9}  {'val_Shrp':>9}{'val_annRet%':>12}")
    print("-" * 47)
    sharpes: dict[str, float] = {}
    for cls in ASSET_CLASSES:
        sub = {s: panel[s] for s in panel if _asset_class(s) != cls}
        classes_left = sorted({_asset_class(s) for s in sub})
        if len(sub) < 2 or len(classes_left) < 2:
            print(f"{cls:<10}{len(sub):>5}{len(classes_left):>9}  (too few to run)")
            continue
        res = run_panel_backtest(sub, HEADLINE_CFG, COST_BPS, stress_factor=1.0)
        m, n_reb, n_taken = _window_metrics(res, train_end, val_end, HEADLINE_CFG.rebalance_days)
        if m is None:
            continue
        sharpes[cls] = m.sharpe_net
        print(f"{cls:<10}{len(sub):>5}{len(classes_left):>9}  "
              f"{m.sharpe_net:>+9.2f}{m.ann_return_net * 100:>+12.2f}")
        log_experiment(
            registry, setup="tsmom_panel", symbol=f"PANEL_drop_{cls}", timeframe="D1", split="val",
            params={"label": f"drop:{cls}", "lookback": HEADLINE_CFG.lookback,
                    "vol_window": HEADLINE_CFG.vol_window,
                    "rebalance_days": HEADLINE_CFG.rebalance_days,
                    "instruments": sorted(sub), "dropped_class": cls},
            metrics=m.as_dict(), data_hash=data_hash, n_signals=n_reb, n_taken=n_taken,
            stressed=False, phase="P4",
        )
    return sharpes


def main() -> None:
    cfg = load_config()
    registry = Path(cfg.experiments_dir) / "registry.jsonl"
    train_end = to_utc_ts(cfg.split.train_end)
    val_end = to_utc_ts(cfg.split.val_end)

    print("\nP4-candidate mechanism #7 — multi-asset TSMOM, PORTFOLIO form "
          "(vol-scaled, return-space)")
    print("frozen hypothesis: diversified vol-scaled 12m-momentum earns "
          "+Sharpe net of costs.")
    print(f"headline: lb={HEADLINE_CFG.lookback}, vw={HEADLINE_CFG.vol_window}, "
          f"reb={HEADLINE_CFG.rebalance_days}, tgt_vol={HEADLINE_CFG.target_ann_vol:.0%} "
          f"(the ONLY cell judged). context reported, not judged.")
    print(f"train<= {train_end.date()} | val ({train_end.date()}, {val_end.date()}] | "
          "TEST (2024+) SEALED at load; nothing tuned toward a positive.\n")

    panel = load_panel(cfg, UNIVERSE, val_end)
    if len(panel) < 2:
        print("panel has < 2 instruments on disk — run scripts/build_tsmom_universe.py first.")
        return
    data_hash = _panel_hash(panel)

    print(f"{'symbol':<8}{'class':<7}{'rows<=val':>10}  span")
    print("-" * 48)
    for s in panel_instruments(panel):
        b = panel[s]
        span = f"{b.index.min().date()}..{b.index.max().date()}"
        print(f"{s:<8}{_asset_class(s):<7}{len(b):>10}  {span}")
    n_by_class = {c: sum(_asset_class(s) == c for s in panel) for c in ASSET_CLASSES}
    print(f"panel: {len(panel)} instruments | by class: "
          + ", ".join(f"{c}={n}" for c, n in n_by_class.items()) + f" | hash {data_hash}")

    # Headline (judged) + declared context (reported only).
    headline = _measure_and_log(
        cfg, registry, panel, HEADLINE_LABEL, HEADLINE_CFG, train_end, val_end, data_hash
    )
    for label, pcfg in CONTEXT_CFGS:
        _measure_and_log(cfg, registry, panel, label, pcfg, train_end, val_end, data_hash)

    # Sub-period OOS blocks (headline) — need the full train+val stream, so re-run normal headline.
    res_hl = run_panel_backtest(panel, HEADLINE_CFG, COST_BPS, stress_factor=1.0)
    blocks, n_pos_blocks = _sub_period_blocks(res_hl, HEADLINE_CFG.rebalance_days)
    print("\n--- sub-period OOS blocks (headline, train+val timeline, nothing fit) ---")
    print(f"{'block':<6}{'from':<12}{'to':<12}{'days':>6}{'annRet%':>10}{'Sharpe':>9}")
    print("-" * 55)
    for b in blocks:
        print(f"{b['block']:<6}{str(b['from']):<12}{str(b['to']):<12}{b['n_days']:>6}"
              f"{b['ann_ret_net'] * 100:>+10.2f}{b['sharpe_net']:>+9.2f}")
    print(f"blocks with net ann return > 0: {n_pos_blocks}/{len(blocks)} "
          f"(strict-majority bar = {SUBPERIOD_MIN}/{N_BLOCKS})")

    # Drop-one-asset-class (headline, val).
    drop_sharpes = _drop_one_class(cfg, registry, panel, train_end, val_end, data_hash)

    # ---- Mechanical, pre-committed P4 checklist (train+val only; classification lives in the ADR).
    m_val = headline.get("val:normal")
    m_val_s = headline.get("val:stress")
    m_tr = headline.get("train:normal")
    checks = {
        "val net Sharpe > 0": bool(m_val and m_val.sharpe_net > 0),
        "val net Sharpe > 0 under +50% stress": bool(m_val_s and m_val_s.sharpe_net > 0),
        f"sub-period robust (>= {SUBPERIOD_MIN}/{N_BLOCKS} net>0)": (
            n_pos_blocks >= SUBPERIOD_MIN
        ),
        "not single-class-dependent (every drop-one-class val Sharpe > 0)": (
            bool(drop_sharpes) and min(drop_sharpes.values()) > 0
        ),
    }
    print("\n######## MECHANICAL P4 CHECKLIST — headline, train+val only (TEST SEALED) ########")
    if m_tr and m_val:
        print(f"headline train net Sharpe {m_tr.sharpe_net:+.2f} | "
              f"val net Sharpe {m_val.sharpe_net:+.2f}"
              + (f" | val stressed {m_val_s.sharpe_net:+.2f}" if m_val_s else ""))
    for name, ok in checks.items():
        print(f"  [{'PASS' if ok else 'FAIL'}]  {name}")
    print(f"\nall mechanical checks pass: {all(checks.values())}")
    print(
        "\nSharpe/Sortino/return are ANNUALIZED and NET of the documented bps turnover costs; "
        "\nmaxDD% is calendar-time peak-to-trough; turn/y is annualized one-way turnover. "
        "\nval = out-of-sample; sub-period blocks are sequential OOS (no parameter is fit). "
        "\nP4 GO needs +net Sharpe, robust sub-periods, +50%-stress-stable, "
        "and NOT dependent on one asset class — on train+val, BEFORE sealed "
        "2024 test is read. This script prints "
        "\nhonest numbers + the mechanical checklist ONLY; the VALID/PLAUSIBLE/WEAK/INVALID "
        "\nclassification and the GO/NO-GO decision are recorded in an ADR after review. No ML."
    )


if __name__ == "__main__":
    main()
