# fxlab — AI-Assisted Forex Trading **Research** Platform

> A research/validation platform first, a trading system last.
> The objective is **expectancy + robustness net of costs**, never win rate.
> Nothing is claimed without a test. See [docs/00-charter.md](docs/00-charter.md).

## Honesty charter (non-negotiable)

- Every result is labelled **HYPOTHESIS** (untested), **RESULT** (tested), or **VALIDATED** (out-of-sample + robust).
- Ideas are classified **VALID / PLAUSIBLE / UNPROVEN / WEAK / INVALID**.
- No fabricated win rates, profits, or backtest numbers — ever.
- The "80–90% accuracy" idea is treated as an **observation to explain if it appears**, never a tuning target.
- A failed phase gate is a legitimate finding — possibly "no edge here".

## Status

**Phase 3 — SMC layer: green (detectors built + measured).** 144 tests pass, including
strict-fractal pivot detection, causal swing-confirmation timing, the BOS/CHoCH state
machine, confirmation-filter composition on Model A, and a future-invariance guard on
**every** structure/SMC detector *and on all three stateful setups (Models C, D, and the
non-SMC Model E)*; `ruff` is clean. Each concept is an objective, unit-tested, causal-by-construction detector: swings +
market structure (HH/HL/LH/LL, BOS, CHoCH), displacement, fair-value gaps, order blocks,
liquidity (equal highs/lows + sweeps), and premium/discount. **Model A — liquidity-sweep
reversal** composes them into the first SMC setup (with optional displacement / FVG /
structure / premium-discount filters); **Model D — FVG-retracement continuation** and
**Model C — breakout-failure (fakeout) reversal** are two deliberately *independent*
mechanisms (both stateful, both still strictly causal).

**Honest P3 result (RESULT, in-sample/train — not VALIDATED):** incremental expectancy of
the SMC setup vs the Model B baseline, net of costs:

- On **H1 train**, Model A shows a small **positive _gross_ edge (+0.042 R/trade, gross
  PF 1.06)** that the trend-pullback baseline lacks (≈0 gross). But it is marginal
  (t ≈ 1.7, in-sample — not significant) and **negative net of costs** (−0.033 R/trade;
  −0.071 under +50% stress): cost drag (~0.075 R/trade) exceeds the gross edge.
- On **M15 train** the gross edge vanishes (+0.0004 R) — not robust across timeframes; net
  is deeply negative (cost drag ~0.17 R because ATR stops are tiny there).
- The premium/discount alignment filter did **not** improve H1 (+0.032 < +0.042 gross).

**Follow-up — lifting the cost-to-edge ratio (P3.1, train + validation).** The one thread
worth pulling was: make the setup more selective (so per-trade edge outgrows the fixed
per-trade cost) and move to a higher timeframe (wider ATR stops → cost is a smaller fraction
of R). Both were built as causal, same-bar confirmation filters on Model A (`--require-
displacement`, `--require-fvg`, `--align-structure`) and measured by
[scripts/measure_cost_to_edge.py](scripts/measure_cost_to_edge.py). **In-sample it worked:**
structure alignment concentrated edge (H1 gross +0.042 → **+0.072**, t 1.65 → 1.92; net
−0.033 → −0.005) and H4 halved cost drag (~0.077 → ~0.036 R), nudging H4 `A +structure` to a
marginally positive net (+0.019, ~breakeven stressed). **Out-of-sample (validation, 2022–23)
it did not hold:** `A +structure` *flipped sign* on H1 val (gross **−0.106, t −1.59**, net
−0.176 — worse than the baseline) while being +0.073 (t 0.74, n=104) on H4 — a real edge does
not reverse between timeframes. The base H1 gross edge itself shrank +0.042 → +0.016 OOS, and
the eye-catching filtered cells were tiny-sample mirages (the `A +fvg` H4 val cell is **one
trade**, +2.0 R). Net: the in-sample lift was a **selection effect that fails on validation**.

**Second follow-up — an independent mechanism (P3.2, Model D).** To check the failure isn't
specific to the sweep-reversal formulation, **Model D — FVG-retracement continuation** trades a
mechanistically distinct idea (price retraces into an unfilled fair-value gap, then resumes)
under the same causal discipline. On the same train+val grid it shows **no edge**: gross
expectancy is ≈0 in-sample (H1 train +0.008 R/trade, t 0.34 — weaker than Model A's +0.042) and
**negative out-of-sample** (H1 val −0.020; H4 val **−0.120, t −1.61**, i.e. mildly
*anti*-predictive even before costs). A fixed 10-pip minimum-gap selectivity floor reproduced
the same in-sample-only bump (H1 train +0.008 → +0.016) that again failed OOS. Net of costs it
loses in every cell.

**Third follow-up — a second independent mechanism (P3.3, Model C).** **Model C —
breakout-failure (fakeout) reversal** fades a *trapped* close-based breakout of a confirmed
structural level (price closes beyond the level, then closes back through it within a short
window). On the same train+val grid it is, if anything, **mildly _anti_-predictive gross
in-sample** — H1 train **−0.047 R/trade (t −1.59)**, H4 train −0.064 — the *opposite* of Model
A's small positive in-sample gross (+0.042). Out of sample the gross collapses to noise around
zero (H1 val +0.004, t 0.07; H4 val −0.012) with the sign flipping between train and val, so
there is no stable signal to keep. Net of costs it loses in **every** cell (−0.044 to −0.121
R/trade, worse stressed), and the stricter `max_wait=3` variant concentrates no edge (H4 val
worsens to −0.075 gross). A second independent mechanism, again carrying none.

So the SMC setups tried so far add only a small, fragile, timeframe-specific *gross* signal that
neither survives realistic costs nor generalizes out of sample — and two independent mechanisms
add none at all.

**Fourth follow-up — cross-pair robustness (P3.4, GBP/USD + USD/JPY).** The P4 gate wants an edge
robust across **≥2 pairs**, so the core mechanisms (B baseline, A base, A +structure, C, D) were
re-run on real GBP/USD and USD/JPY data, train+val, with
[scripts/measure_cross_pair.py](scripts/measure_cross_pair.py). It did not rescue P4 — it *further
undermined* the mechanisms. **Model A's small EUR/USD gross edge is pair-specific: it does not
replicate** — `A base` is negative gross in *every* GBP/USD and USD/JPY cell, significantly so out
of sample (GBP val H1 −0.096, t −2.15; JPY val H1 −0.084, t −1.80). The confirmation filters and
Model C sign-flip across pairs with no stable side. The one mild lead is **Model D**, which leans
positive gross on USD/JPY (all four cells) and is even net-positive on **USD/JPY H4 in both train
(+0.029) and val (+0.133, +0.122 stressed), same side, PF 1.10/1.26** — but it *fails on EUR/USD*
(val H4 net −0.152), is net-negative on H1 everywhere (cost drag), and every t-stat is <2. That
the naive Model B baseline itself throws sign-*flipping* |t|>2 cells (GBP H1 train +0.076 t 2.23 →
val −0.119 t −2.09) shows the single-cell noise floor. So Model D on USD/JPY H4 is a **WEAK,
sub-significant, single-pair lead** — per the charter an observation to investigate, never a
target — not a P4 pass.

**Fifth follow-up — investigating that lone lead (P3.4b, the one edge-like cell).** Rather than
stop at "WEAK," the single most edge-like result — Model D on USD/JPY H4 — was decomposed to see
*why* it looked that way and whether it survives, strictly on train + validation (test still
sealed), tuning nothing:
[scripts/investigate_jpy_d.py](scripts/investigate_jpy_d.py) (side split, always-long/short
directional benchmarks, and a 1000-draw random-timing placebo with matched side counts). The prior
suspicion — that a *continuation* setup's USD/JPY edge was just directional beta to the yen decline
— was **refuted**: on train USD/JPY drifted **−4.0%** yet Model D's gain sat on the **short** side
(+0.102, beating the always-short benchmark −0.027), and on val (USD/JPY **+22.4%**) its shorts made
**+0.137** while blind shorting lost **−0.149** — a short book profitable in a 22% uptrend is the
opposite of beta. What remains is a **short-biased single-pair anomaly** (short-side net positive in
all three train sub-periods and on val) whose entry timing beats ~90% of same-side random portfolios
on USD/JPY H4 — but only **marginally** (z ≈ +1.3 to +1.6, sub-2σ) and **only there**: on EUR/USD H4
val Model D sits at the **5th percentile** of its own null (z −1.62, *worse* than random). So the
investigation *sharpened* the anomaly rather than dissolving it, yet it still **does not survive** as
a robust edge — one pair only, marginal significance, short-biased. It stays **WEAK**: an
observation to investigate later, never a target. Recorded in
[docs/adr/0002-usdjpy-h4-model-d-lead-investigated.md](docs/adr/0002-usdjpy-h4-model-d-lead-investigated.md).

**Sixth follow-up — a genuinely new mechanism *outside* SMC (P4-candidate #5, Model E).** With the
whole SMC universe (A–D) rejected, the only remaining pre-ML avenue was a mechanism unrelated to
Smart-Money Concepts. **Model E — session opening-range breakout** is a classic time-of-day
*volatility* idea: when a major session opens (canonically **London, 07:00–16:00 UTC**), break the
session's opening range in the breakout direction. It was built fresh (not seeded by the SMC
results), unit-tested for causality/future-invariance (14 tests), and measured with the identical
pipeline by [scripts/measure_session_breakout.py](scripts/measure_session_breakout.py) — real
Dukascopy, all three pairs on **H1 and M15** (H4 excluded: a session is ~2 H4 bars, so there is no
opening range), train + validation, test still sealed. The **pre-registered headline** was one
cell-family (London, `or_bars=1`); a wider 2-bar range and the New York open were declared up front
as robustness context, not a search. **It fails P4.** Headline net expectancy is **negative in 11 of
12 cells** (−0.06 to −0.19 R/trade), gross is ≈0 almost everywhere (in-sample |t| < 0.8 in 10/12 — the
breakout is a near coin-flip on direction before costs), and a purged **walk-forward is net-negative
in all six pair/TF timelines** (t −2.6 to −5.8; even the friendliest, USD/JPY H1, is −0.024 overall).
The single net-positive cell — **USD/JPY val H1** (+0.085, gross t +2.10) — is a **long-side-only**
gain during the **+22.5%** 2022–2023 yen move that is *absent* in USD/JPY train, *absent* on USD/JPY
M15, and negative on both other pairs: the **same pair+period footprint** where the unrelated Model D
also popped (ADR 0002). Two independent mechanisms lighting up on exactly USD/JPY-2022/23 is a
**regime artifact**, not a generalizable edge. Model E as a standalone edge → **INVALID**; the lone
cell → a **WEAK observation**, never a target. Recorded in
[docs/adr/0003-model-e-session-breakout-rejected.md](docs/adr/0003-model-e-session-breakout-rejected.md).

**Seventh follow-up — a structurally-motivated mechanism at a *new horizon* (P4-candidate #6, Model
F).** With five intraday mechanisms down, the bar for trying another was a **prior structural reason**,
not another chart pattern. **Model F — time-series momentum (trend-following)** clears that bar: an
asset's own trailing return predicting its next move is the single most-replicated cross-asset premium
in the literature (Moskowitz–Ooi–Pedersen 2012; Hurst–Ooi–Pedersen, "A Century of Evidence"),
attributed in FX to slow diffusion of macro/monetary information. It is genuinely different from A–E on
two axes: **horizon** (daily/multi-week, not intraday) and **cost structure** (on D1 the ATR stop is
~10× larger, so fixed per-trade cost is a small fraction of R — the exact dimension that killed Model
E). D1 bars are a deterministic resample of the stored H1
([scripts/build_daily_bars.py](scripts/build_daily_bars.py)); the setup
([src/fxlab/setups/model_f_momentum.py](src/fxlab/setups/model_f_momentum.py)) is strictly causal (9
unit tests incl. future-invariance) and was measured by
[scripts/measure_momentum.py](scripts/measure_momentum.py) on all three pairs, D1 (headline) + H4
(context), train + validation, test still sealed. The **pre-registered headline** was one cell — D1,
lookback 126 (~6 months); lookbacks 63/252 and H4 were declared up front as robustness context. **It
fails P4.** The headline is a sign-flip on EUR/USD (train **+0.050** → val **−0.115**), noise-level
positive on GBP/USD (both periods, but t ≈ 0.2, one pair), and negative on USD/JPY in both — so it
never clears "positive same-side in train AND val on >1 pair." The purged **walk-forward** is a |t| <
0.7 coin-flip on D1 (each pair only 2/5 OOS blocks positive) and net-**negative** on every H4 timeline.
The structural cost prior was *validated* — D1 cost drag ~0.008–0.016 R/trade vs Model E's ~0.075–0.17
R, a ~5–20× reduction exactly as predicted — but it is **insufficient**: there is no gross directional
edge for the low cost to protect (headline gross |t| < 0.9 everywhere). The only edge-like cell is
again the **USD/JPY-2022/23 footprint** (F-mom63, self-contradictory across timeframes) — the same
regime signal as Models D and E, never the headline. Model F as a standalone edge → **INVALID**; it
does *not* refute TSMOM in general, which needs a broad multi-asset cross-section + vol-scaling this
single-pair, fixed-barrier engine does not provide. Recorded in
[docs/adr/0004-model-f-momentum-rejected.md](docs/adr/0004-model-f-momentum-rejected.md).

So across the plan's full setup universe (Models A–D) and all three plan pairs, these SMC setups
add only a small, fragile, pair/timeframe-specific *gross* signal that neither survives realistic
costs nor generalizes — **and the two genuinely new, non-SMC mechanisms tried since (Model E intraday
volatility, Model F daily momentum) fail too**.

**Eighth follow-up — the structurally-correct TSMOM build (P4-candidate #7, multi-asset portfolio).**
ADR 0004 noted that Model F's failure was *consistent with* the literature, which finds TSMOM's premium
thin per-market and reliant on **(a) diversification across many low-correlation instruments, (b)
volatility-scaled sizing, (c) portfolio-level accounting** — none of which the single-instrument,
three-correlated-FX-major engine provides. P4-candidate #7 was built specifically to test whether the
structurally-motivated, literature-prescribed form of TSMOM survives when constructed correctly: a
**14-instrument cross-asset panel** (7 FX majors + 2 metals + 2 energy + 3 equity indices),
**vol-scaled** (diagonal, equal ex-ante risk per instrument), **return-space portfolio accounting**,
real Dukascopy daily data, measured by
[scripts/measure_tsmom_portfolio.py](scripts/measure_tsmom_portfolio.py) with dedicated machinery
([src/fxlab/backtest/panel.py](src/fxlab/backtest/panel.py),
[src/fxlab/backtest/portfolio_metrics.py](src/fxlab/backtest/portfolio_metrics.py)). The
**pre-registered headline** (12-month lookback, monthly rebalance, 10% vol target) shows **validation
Sharpe +0.39** (stress +0.38, both positive ✅), but fails the **sub-period robustness** check: only
**2 of 6** sequential OOS blocks are positive (need ≥4/6). The positive validation result is
**concentrated in blocks 4–5 (2019–2022)** — the post-COVID trends + yen/energy moves — while blocks
1, 2, 3, and 6 (2015–2018 range + 2022–2023 reversal) are flat-to-negative. This is **regime-dependent**
performance, not a stable cross-period edge. The drop-one-asset-class check passes (all positive ✅),
so it does not hang on one class. Classification: **WEAK** (positive OOS Sharpe, stress-stable, not
single-class-dependent, but **sub-period fragile**). The structural prior was **correct** (this *is*
the right way to test TSMOM), but the **universe size (14) + lack of regime conditioning** left the
premium too thin and too regime-dependent to meet the platform's **strict-majority (4/6) sub-period
robustness bar**. Recorded in
[docs/adr/0005-tsmom-portfolio-rejected.md](docs/adr/0005-tsmom-portfolio-rejected.md).

Per the charter this is a **NO-GO for P4 — now confirmed across seven mechanisms** (four SMC A–D, two
intraday non-SMC E/F, one daily single-instrument non-SMC F, and now one **multi-asset portfolio-form**
non-SMC TSMOM), recorded as decisions: [ADR 0001](docs/adr/0001-p4-no-go-smc-rule-baseline.md),
[ADR 0002](docs/adr/0002-usdjpy-h4-model-d-lead-investigated.md),
[ADR 0003](docs/adr/0003-model-e-session-breakout-rejected.md),
[ADR 0004](docs/adr/0004-model-f-momentum-rejected.md), and
[ADR 0005](docs/adr/0005-tsmom-portfolio-rejected.md). P4 needs positive expectancy *net of costs* on
the **untouched test set**, robust across ≥2 pairs and ≥2 periods (or ≥4/6 sub-periods for a
portfolio), stable under +50% stress; nothing clears that bar, so the test window **stays sealed**
(there is nothing worth spending it on). A failed gate is a legitimate finding — indeed the platform's
core job is the discipline to reach an honest "no robust edge here" and stop. The edge-like cells
(Model D on USD/JPY H4, Model E on USD/JPY val H1, TSMOM blocks 4–5) point at **favourable regimes**
(USD/JPY 2022–2023; post-COVID 2019–2022), not setup edges, and are parked as WEAK observations. So the
pre-ML rule-baseline research **remains concluded at P4**: no robust edge, no ML is built (no ML before
a P4 GO), and the test window stays sealed. No numbers are fabricated. See phase gates in
[docs/00-charter.md](docs/00-charter.md#3-phase-gates).

## Quickstart

```bash
# 1. Create the environment (downloads a managed CPython 3.12 if needed)
python -m uv sync

# 2. Run the test suite (includes leakage / no-look-ahead regression tests)
python -m uv run pytest

# 3. Generate synthetic data offline and run the P1 pipeline end-to-end
python -m uv run fxlab ingest --pair EURUSD --tf M5 --synthetic --bars 20000
python -m uv run fxlab validate-data --pair EURUSD --tf M5
python -m uv run fxlab label --pair EURUSD --tf M5
python -m uv run fxlab split --pair EURUSD --tf M5

# Real data (network; installs the optional extra):
#   python -m uv sync --extra data
#   python -m uv run fxlab ingest --pair EURUSD --tf H4,M15,M5 --from 2015-01-01 --to 2025-01-01

# 4. Backtest a setup on real data — net of costs, logged (no edge is claimed)
python -m uv run fxlab backtest --pair EURUSD --tf H1 --setup model_b --split train --stress
python -m uv run fxlab backtest --pair EURUSD --tf H1 --setup model_a --split train --stress
# ...with a causal confirmation filter (measured, did not survive validation — see Status):
python -m uv run fxlab backtest --pair EURUSD --tf H4 --setup model_a --align-structure --stress
# ...an independent mechanism, FVG-retracement continuation (also no edge — see Status):
python -m uv run fxlab backtest --pair EURUSD --tf H1 --setup model_d --split train --stress
# ...a second independent mechanism, breakout-failure / fakeout reversal (also no edge):
python -m uv run fxlab backtest --pair EURUSD --tf H1 --setup model_c --split train --stress
# ...a NON-SMC mechanism, session opening-range breakout (also no edge — see Status):
python -m uv run fxlab backtest --pair EURUSD --tf H1 --setup model_e --session London --split train --stress
# ...a NON-SMC daily-horizon mechanism, time-series momentum (also no edge — see Status):
python -m uv run python scripts/build_daily_bars.py   # resample stored H1 -> D1 first
python -m uv run fxlab backtest --pair EURUSD --tf D1 --setup model_f --lookback 126 --split train --stress

# Reproduce the P3.1 cost-to-edge grid (train + validation; the test window stays sealed)
python -m uv run python scripts/measure_cost_to_edge.py
# Reproduce the Model E session-breakout grid + walk-forward (train + validation; test sealed)
python -m uv run python scripts/measure_session_breakout.py
# Reproduce the Model F momentum grid + walk-forward (needs build_daily_bars.py first; test sealed)
python -m uv run python scripts/measure_momentum.py
```

> If `uv` is on your PATH you can drop the `python -m` prefix.

### First-run note (Windows + uv managed Python)

If the very first `python -m uv sync` fails with
`Missing expected target directory for Python minor version link`, uv could not create a
symlink for its managed CPython (on Windows, symlinks need Developer Mode or elevated
rights). The interpreter is still downloaded and usable — one-time workaround: build the
venv directly from it, then sync into that venv:

```bash
# Git Bash: locate the CPython 3.12 uv already extracted, then create the venv from it
PYEXE=$(ls -d "$APPDATA/uv/python/cpython-3.12"*"/python.exe" | head -1)
python -m uv venv --python "$PYEXE"
python -m uv sync
```

Once `.venv` exists, `python -m uv sync` and `python -m uv run …` work normally. Enabling
Windows Developer Mode is the permanent fix.

## Layout

See [docs/01-architecture.md](docs/01-architecture.md). Source lives in `src/fxlab/`,
tests in `tests/`, typed config in `config/*.yaml`, design docs in `docs/`.
