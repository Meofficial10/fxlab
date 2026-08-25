"""fxlab command-line interface (Phases 1–3).

Commands: ``info``, ``ingest``, ``validate-data``, ``label``, ``split``, ``backtest``.
Everything runs offline with ``--synthetic``; real data uses the ``dukascopy`` source.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import typer
from rich.console import Console
from rich.table import Table

from .backtest.engine import BacktestConfig, run_backtest
from .backtest.metrics import compute_metrics
from .config import load_config
from .costs.model import CostModel
from .data.ingest_dukascopy import ingest as ingest_data
from .data.resample import resample_ohlcv
from .data.schema import _TF_MINUTES
from .data.store import load_bars, save_bars
from .data.validate import validate_bars
from .experiment.log import hash_bars, log_experiment
from .labeling.triple_barrier import apply_triple_barrier
from .setups.model_a_sweep_reversal import ModelASweepReversal
from .setups.model_b_trend_pullback import ModelBTrendPullback
from .setups.model_c_breakout_failure import ModelCBreakoutFailure
from .setups.model_d_fvg_retracement import ModelDFvgRetracement
from .setups.model_e_session_breakout import ModelESessionBreakout
from .setups.model_f_momentum import ModelFMomentum
from .validation.splits import chronological_split

# Setup registry: name -> factory. New setups (more SMC) register here.
_SETUPS = {
    "model_a": ModelASweepReversal,
    "model_a_sweep_reversal": ModelASweepReversal,
    "model_b": ModelBTrendPullback,
    "model_b_trend_pullback": ModelBTrendPullback,
    "model_c": ModelCBreakoutFailure,
    "model_c_breakout_failure": ModelCBreakoutFailure,
    "model_d": ModelDFvgRetracement,
    "model_d_fvg_retracement": ModelDFvgRetracement,
    "model_e": ModelESessionBreakout,
    "model_e_session_breakout": ModelESessionBreakout,
    "model_f": ModelFMomentum,
    "model_f_momentum": ModelFMomentum,
}

app = typer.Typer(add_completion=False, help="fxlab — forex research platform (Phase 1).")
console = Console()


def _tf_list(tf: str) -> list[str]:
    return [t.strip() for t in tf.split(",") if t.strip()]


@app.command()
def info() -> None:
    """Print the resolved, validated configuration."""
    cfg = load_config()
    console.print(f"[bold]{cfg.project_name}[/bold]  data_dir={cfg.data_dir}")
    console.print(f"symbols={cfg.data.symbols}  timeframes={cfg.data.timeframes}")
    console.print(f"source={cfg.data.source}")
    console.print(
        f"split: train<= {cfg.split.train_end}, val<= {cfg.split.val_end}, "
        f"test> {cfg.split.val_end}"
    )
    console.print(
        f"label: tp={cfg.label.tp_atr_mult}xATR sl={cfg.label.sl_atr_mult}xATR "
        f"max_hold={cfg.label.max_hold_bars} atr_window={cfg.label.atr_window}"
    )


@app.command()
def ingest(
    pair: str = typer.Option(..., "--pair"),
    tf: str = typer.Option("M5", "--tf", help="one or comma-separated, e.g. H4,M15,M5"),
    synthetic: bool = typer.Option(False, "--synthetic", help="offline deterministic data"),
    source: str = typer.Option(None, "--source", help="override config source"),
    frm: str = typer.Option(None, "--from"),
    to: str = typer.Option(None, "--to"),
    bars: int = typer.Option(20_000, "--bars", help="synthetic bar count (base TF)"),
    seed: int = typer.Option(7, "--seed"),
) -> None:
    """Ingest bars, save to parquet, and validate."""
    cfg = load_config()
    src = "synthetic" if synthetic else (source or cfg.data.source)
    tfs = _tf_list(tf)

    frames: dict[str, object] = {}
    if src == "synthetic":
        base_tf = min(tfs, key=lambda t: _TF_MINUTES[t])
        base = ingest_data(
            pair, base_tf, source="synthetic", n_bars=bars,
            start=frm or "2018-01-01", seed=seed,
        )
        for t in tfs:
            frames[t] = base if t == base_tf else resample_ohlcv(base, t)
    else:
        for t in tfs:
            frames[t] = ingest_data(pair, t, source=src, start=frm, end=to)

    failed = False
    for t, df in frames.items():
        path = save_bars(df, cfg.data_dir, pair, t)
        rep = validate_bars(df, t, pair)
        console.print(f"[green]ingested[/green] {len(df):>7} {pair}/{t} from {src} -> {path}")
        console.print(rep.summary())
        failed = failed or not rep.ok
    if failed:
        raise typer.Exit(1)


@app.command("validate-data")
def validate_data(
    pair: str = typer.Option(..., "--pair"),
    tf: str = typer.Option("M5", "--tf"),
) -> None:
    """Validate previously-ingested bars."""
    cfg = load_config()
    ok = True
    for t in _tf_list(tf):
        rep = validate_bars(load_bars(cfg.data_dir, pair, t), t, pair)
        console.print(rep.summary())
        ok = ok and rep.ok
    if not ok:
        raise typer.Exit(1)


@app.command()
def label(
    pair: str = typer.Option(..., "--pair"),
    tf: str = typer.Option("M5", "--tf"),
    every: int = typer.Option(50, "--every", help="place a demo signal every N bars"),
) -> None:
    """P1 WIRING DEMO: label placeholder signals with the triple-barrier method.

    The signals here are NOT a strategy and imply NO edge — they only exercise the
    labeling + cost pipeline. Real setups arrive in Phase 2.
    """
    cfg = load_config()
    lc = cfg.label
    df = load_bars(cfg.data_dir, pair, tf)
    n = len(df)
    sig = np.arange(lc.atr_window + 1, n - lc.max_hold_bars - 1, every)
    if len(sig) == 0:
        console.print("[yellow]not enough bars for a demo — ingest more[/yellow]")
        raise typer.Exit(1)

    closes = df["close"].to_numpy()
    side = np.where(closes[sig] >= closes[sig - 1], 1, -1)  # 1-bar momentum placeholder
    cm = CostModel.from_config(cfg.costs, pair)
    res = apply_triple_barrier(
        df, sig, side,
        tp_mult=lc.tp_atr_mult, sl_mult=lc.sl_atr_mult, max_hold=lc.max_hold_bars,
        atr_window=lc.atr_window, latency_bars=cm.latency_bars, cost_model=cm,
    )
    if res.empty:
        console.print("[yellow]no labelled events[/yellow]")
        raise typer.Exit(1)

    counts = res["outcome"].value_counts().to_dict()
    table = Table(title=f"Triple-barrier demo — {pair}/{tf}  (n={len(res)})  [NOT A STRATEGY]")
    table.add_column("metric")
    table.add_column("value", justify="right")
    n_tp, n_sl, n_to = counts.get("tp", 0), counts.get("sl", 0), counts.get("timeout", 0)
    table.add_row("TP / SL / timeout", f"{n_tp} / {n_sl} / {n_to}")
    table.add_row("label rate (TP-first)", f"{res['label'].mean():.3f}")
    table.add_row("mean gross (pips)", f"{res['gross_ret'].mean() / cm.pip_size:+.2f}")
    table.add_row("mean net (pips)", f"{res['net_ret'].mean() / cm.pip_size:+.2f}")
    table.add_row("avg bars held", f"{res['bars_held'].mean():.1f}")
    console.print(table)
    console.print("[dim]Descriptive wiring output only — no expectancy/edge is claimed.[/dim]")


@app.command()
def split(
    pair: str = typer.Option(..., "--pair"),
    tf: str = typer.Option("M5", "--tf"),
) -> None:
    """Show the chronological train/val/test split (test window stays untouched until P4)."""
    cfg = load_config()
    df = load_bars(cfg.data_dir, pair, tf)
    sp = chronological_split(df.index, cfg.split.train_end, cfg.split.val_end)
    table = Table(title=f"Chronological split — {pair}/{tf}")
    table.add_column("cut")
    table.add_column("bars", justify="right")
    table.add_column("from")
    table.add_column("to")
    for name, idx in (("train", sp.train), ("val", sp.val), ("test", sp.test)):
        if len(idx):
            table.add_row(name, str(len(idx)), str(idx.min()), str(idx.max()))
        else:
            table.add_row(name, "0", "-", "-")
    console.print(table)
    if len(sp.val) == 0 or len(sp.test) == 0:
        console.print(
            "[dim]note: synthetic demo data may not span the configured split dates.[/dim]"
        )


def _select_split(
    df: pd.DataFrame, cfg, split_name: str, allow_test: bool
) -> pd.DataFrame:
    """Return the bar slice for a named split, re-attaching symbol/timeframe attrs.

    The TEST window stays sealed until Phase 4: selecting it requires ``--allow-test``,
    and any such access is recorded in the experiment log by the caller.
    """
    sp = chronological_split(df.index, cfg.split.train_end, cfg.split.val_end)
    table = {"train": sp.train, "val": sp.val, "test": sp.test, "all": df.index}
    if split_name not in table:
        raise typer.BadParameter(f"unknown split {split_name!r}; choose {sorted(table)}")
    if split_name == "test" and not allow_test:
        console.print(
            "[red]refusing to touch the TEST window — it is sealed until Phase 4.[/red]\n"
            "[dim]pass --allow-test only when you deliberately mean to spend it (logged).[/dim]"
        )
        raise typer.Exit(2)
    sub = df.loc[table[split_name]].copy()
    sub.attrs.update(df.attrs)
    return sub


def _fmt(x: float, nd: int = 3) -> str:
    if x != x:  # NaN
        return "-"
    if x in (float("inf"), float("-inf")):
        return "inf" if x > 0 else "-inf"
    return f"{x:+.{nd}f}"


def _run_and_report(bars, sig, side, cm, bt_cfg, label: str):
    res = run_backtest(bars, sig, side, cm, bt_cfg)
    m = compute_metrics(res.trades)
    table = Table(title=f"Backtest {label} - {res.symbol}/{res.timeframe}  (net of costs)")
    table.add_column("metric")
    table.add_column("net", justify="right")
    table.add_column("gross", justify="right")
    table.add_row("trades taken", str(m.n_trades), f"of {res.n_signals} signals")
    table.add_row("win rate", f"{m.win_rate:.3f}" if m.n_trades else "-", "(not the objective)")
    table.add_row("expectancy /trade (R)", _fmt(m.expectancy_R_net), _fmt(m.expectancy_R_gross))
    table.add_row("expectancy /trade (pips)", _fmt(m.expectancy_pips_net, 2),
                  _fmt(m.expectancy_pips_gross, 2))
    table.add_row("avg win / loss (R)", _fmt(m.avg_win_R_net), _fmt(m.avg_loss_R_net))
    table.add_row("profit factor", _fmt(m.profit_factor_net, 2), _fmt(m.profit_factor_gross, 2))
    table.add_row("total (R)", _fmt(m.total_R_net, 1), _fmt(m.total_R_gross, 1))
    table.add_row("max drawdown (R)", f"{m.max_drawdown_R:.1f}", "")
    table.add_row("longest win / loss streak",
                  f"{m.longest_win_streak} / {m.longest_loss_streak}", "")
    table.add_row("cost drag /trade (R)", f"{m.cost_drag_R_per_trade:.4f}", "")
    console.print(table)
    return res, m


def _build_setup(setup: str, *, ema_fast: int, ema_slow: int, left: int, right: int,
                 align_pd: bool, require_displacement: bool, require_fvg: bool,
                 align_structure: bool, body_mult: float, min_gap: float, max_age: int,
                 max_wait: int, session_start_hour: int, session_end_hour: int,
                 or_bars: int, session_max_watch: int, session_label: str, lookback: int):
    """Construct a setup and return (strategy, params-for-logging) with only its own knobs."""
    if setup in ("model_a", "model_a_sweep_reversal"):
        strat = ModelASweepReversal(
            left=left, right=right, align_pd=align_pd,
            require_displacement=require_displacement, require_fvg=require_fvg,
            align_structure=align_structure, body_mult=body_mult,
        )
        params = {
            "setup": strat.name, "left": left, "right": right, "align_pd": align_pd,
            "require_displacement": require_displacement, "require_fvg": require_fvg,
            "align_structure": align_structure, "body_mult": body_mult,
        }
    elif setup in ("model_b", "model_b_trend_pullback"):
        strat = ModelBTrendPullback(ema_fast=ema_fast, ema_slow=ema_slow)
        params = {"setup": strat.name, "ema_fast": ema_fast, "ema_slow": ema_slow}
    elif setup in ("model_c", "model_c_breakout_failure"):
        strat = ModelCBreakoutFailure(left=left, right=right, max_wait=max_wait)
        params = {"setup": strat.name, "left": left, "right": right, "max_wait": max_wait}
    elif setup in ("model_d", "model_d_fvg_retracement"):
        strat = ModelDFvgRetracement(min_gap=min_gap, max_age=max_age)
        params = {"setup": strat.name, "min_gap": min_gap, "max_age": max_age}
    elif setup in ("model_e", "model_e_session_breakout"):
        strat = ModelESessionBreakout(
            start_hour=session_start_hour, end_hour=session_end_hour,
            or_bars=or_bars, max_watch=session_max_watch,
        )
        params = {
            "setup": strat.name, "session": session_label,
            "start_hour": session_start_hour, "end_hour": session_end_hour,
            "or_bars": or_bars, "max_watch": session_max_watch,
        }
    elif setup in ("model_f", "model_f_momentum"):
        strat = ModelFMomentum(lookback=lookback)
        params = {"setup": strat.name, "lookback": lookback}
    else:  # pragma: no cover - guarded by the caller's registry check
        raise typer.BadParameter(f"unknown setup {setup!r}; choose {sorted(_SETUPS)}")
    return strat, params


@app.command()
def backtest(
    pair: str = typer.Option(..., "--pair"),
    tf: str = typer.Option("M5", "--tf"),
    setup: str = typer.Option("model_b", "--setup", help=f"one of {sorted(_SETUPS)}"),
    split_name: str = typer.Option("train", "--split", help="train | val | test | all"),
    stress: bool = typer.Option(False, "--stress", help="also run at +50% cost stress"),
    ema_fast: int = typer.Option(20, "--ema-fast", help="model_b: fast EMA span"),
    ema_slow: int = typer.Option(50, "--ema-slow", help="model_b: slow EMA span"),
    left: int = typer.Option(2, "--left", help="model_a/model_c: swing left span"),
    right: int = typer.Option(2, "--right", help="model_a/model_c: swing right span"),
    align_pd: bool = typer.Option(
        False, "--align-pd", help="model_a: longs only in discount, shorts only in premium"
    ),
    require_displacement: bool = typer.Option(
        False, "--require-displacement", help="model_a: sweep bar must displace (reversal dir)"
    ),
    require_fvg: bool = typer.Option(
        False, "--require-fvg", help="model_a: FVG completes on the sweep bar (reversal dir)"
    ),
    align_structure: bool = typer.Option(
        False, "--align-structure", help="model_a: sweep dir must match market-structure trend"
    ),
    body_mult: float = typer.Option(
        1.5, "--body-mult", help="model_a: displacement filter body/ATR threshold"
    ),
    min_gap: float = typer.Option(
        0.0, "--min-gap", help="model_d: ignore fair-value gaps narrower than this (price)"
    ),
    max_age: int = typer.Option(
        500, "--max-age", help="model_d: bars an unfilled gap stays active before expiring"
    ),
    max_wait: int = typer.Option(
        10, "--max-wait", help="model_c: bars a breakout is watched for a reclaim before expiring"
    ),
    session: str = typer.Option(
        "London", "--session", help="model_e: session whose opening range to break (from config)"
    ),
    or_bars: int = typer.Option(
        1, "--or-bars", help="model_e: bars forming the opening range"
    ),
    session_max_watch: int = typer.Option(
        24, "--session-max-watch", help="model_e: max bars after the OR to watch for a breakout"
    ),
    lookback: int = typer.Option(
        126, "--lookback", help="model_f: trailing bars for the momentum sign (D1: 126 ~= 6 months)"
    ),
    allow_test: bool = typer.Option(False, "--allow-test", help="unseal the P4 test window"),
) -> None:
    """P2/P3 baseline: backtest an objective setup net of costs and log the experiment.

    Runs on TRAIN by default. Reports the full metric set GROSS and NET. No edge is
    assumed or claimed — the numbers are whatever they honestly are.
    """
    cfg = load_config()
    if setup not in _SETUPS:
        raise typer.BadParameter(f"unknown setup {setup!r}; choose {sorted(_SETUPS)}")

    # Resolve the model_e session name to its fixed-UTC window from config (default London).
    _sessions = {s.name: (s.start_hour, s.end_hour) for s in cfg.data.sessions}
    session_start_hour, session_end_hour = _sessions.get(session, (7, 16))

    df_full = load_bars(cfg.data_dir, pair, tf)
    bars = _select_split(df_full, cfg, split_name, allow_test)
    if len(bars) < cfg.label.atr_window + cfg.label.max_hold_bars + 5:
        console.print("[yellow]not enough bars in this split — ingest more or widen it[/yellow]")
        raise typer.Exit(1)

    strat, params = _build_setup(
        setup, ema_fast=ema_fast, ema_slow=ema_slow, left=left, right=right, align_pd=align_pd,
        require_displacement=require_displacement, require_fvg=require_fvg,
        align_structure=align_structure, body_mult=body_mult, min_gap=min_gap, max_age=max_age,
        max_wait=max_wait, session_start_hour=session_start_hour,
        session_end_hour=session_end_hour, or_bars=or_bars,
        session_max_watch=session_max_watch, session_label=session, lookback=lookback,
    )
    sig, side = strat.generate(bars)
    cm = CostModel.from_config(cfg.costs, pair)
    bt_cfg = BacktestConfig.from_label_config(cfg.label, latency_bars=cm.latency_bars)

    console.print(
        f"[bold]{strat.name}[/bold] on {pair}/{tf} split={split_name}  "
        f"({bars.index.min()} .. {bars.index.max()}, {len(bars)} bars)"
    )
    if split_name == "test":
        console.print("[red]>>> TEST WINDOW ACCESSED (P4 gate) — this is being logged.[/red]")

    data_hash = hash_bars(bars)
    registry = Path(cfg.experiments_dir) / "registry.jsonl"
    params = {
        **params,
        "tp_atr_mult": bt_cfg.tp_atr_mult, "sl_atr_mult": bt_cfg.sl_atr_mult,
        "max_hold_bars": bt_cfg.max_hold_bars, "atr_window": bt_cfg.atr_window,
        "latency_bars": bt_cfg.latency_bars,
    }

    res, m = _run_and_report(bars, sig, side, cm, bt_cfg, "(normal costs)")
    log_experiment(
        registry, setup=strat.name, symbol=pair, timeframe=tf, split=split_name,
        params=params, metrics=m.as_dict(), data_hash=data_hash,
        n_signals=res.n_signals, n_taken=res.n_taken, stressed=False,
    )

    if stress:
        cm_s = cm.stress(cfg.costs.stress_factor)
        stress_pct = (cfg.costs.stress_factor - 1) * 100
        res_s, m_s = _run_and_report(
            bars, sig, side, cm_s, bt_cfg, f"(+{stress_pct:.0f}% cost stress)"
        )
        log_experiment(
            registry, setup=strat.name, symbol=pair, timeframe=tf, split=split_name,
            params={**params, "stress_factor": cfg.costs.stress_factor},
            metrics=m_s.as_dict(), data_hash=data_hash,
            n_signals=res_s.n_signals, n_taken=res_s.n_taken, stressed=True,
        )

    console.print(
        f"[dim]logged -> {registry}  (data_hash={data_hash}). "
        "HYPOTHESIS baseline; expectancy net of costs is the only target, never win rate.[/dim]"
    )


if __name__ == "__main__":
    app()
