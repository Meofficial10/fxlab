# ADR 0004 — Model F (time-series momentum / trend-following, non-SMC, daily) rejected at P4

- **Status:** Accepted (2026-08-24). Extends [ADR 0001](0001-p4-no-go-smc-rule-baseline.md),
  [ADR 0002](0002-usdjpy-h4-model-d-lead-investigated.md), and
  [ADR 0003](0003-model-e-session-breakout-rejected.md) — P4 remains **NO-GO**.
- **Phase gate:** P4 Statistical testing (charter [§3](../00-charter.md#3-phase-gates))
- **Decision:** **Model F — time-series momentum (trend-following)**, chosen for a documented
  *structural* prior and tested at a **daily / multi-week horizon** (the first mechanism off the
  intraday timeframes), was built and measured with the identical discipline and held to the
  identical P4 gate. It **fails P4** (no robust net edge on the same side across ≥2 pairs and ≥2
  periods). **Reject it.** No ML is built; the test window (2024+) **stays sealed**.

## Context

After five intraday mechanisms failed (four SMC A–D + the non-SMC session breakout E), the standing
instruction was to try one more mechanism **only if motivated by a prior structural reason**, not to
keep inventing patterns (which is a multiple-comparisons trap). Model F was chosen precisely on that
basis:

**Structural prior (stated before any measurement).** Time-series momentum — an asset's own trailing
return predicting its next move — is the single most-replicated cross-asset premium in the literature
(Moskowitz–Ooi–Pedersen 2012, "Time Series Momentum"; Hurst–Ooi–Pedersen, "A Century of Evidence on
Trend-Following"). In FX it is attributed to slow diffusion of macro/monetary information and
underreaction — an *economic* mechanism, not a chart shape. It is **genuinely different** from A–E on
two axes: (1) horizon — A–E are intraday (hours); momentum lives at a multi-week-to-month horizon on
**daily** bars; (2) **cost structure** — on D1 the ATR-scaled 1R stop is ~10× larger than intraday,
so the fixed per-trade spread+commission is a small fraction of R. That second point is a *specific,
testable* prediction: it is the exact dimension on which Model E died (E's gross ≈ 0 was swamped by
~0.075 R/trade cost drag on H1, ~0.17 R on M15).

**Frozen hypothesis (UNPROVEN — measured, never tuned toward):** the sign of a pair's trailing
`lookback`-bar return predicts the sign of its next move, so entering in that direction carries
positive expectancy net of costs at the daily horizon.

The **pre-registered headline** is exactly one cell: **D1, lookback = 126 (~6 months)** — a canonical
TSMOM horizon fixed *a priori* for horizon-fit and sample adequacy, not chosen on results. Lookbacks
63 and 252, and the H4 timeframe, were declared up front purely as robustness context. Model B is the
no-edge baseline. Every cell is reported; only the headline is judged.

All numbers are RESULTs on **real Dukascopy** data, train (≤2021-12-31) + validation (2022–2023)
only; the test window is never read. D1 bars are a deterministic resample of the stored H1
(`scripts/build_daily_bars.py`; ~3,128 D1 bars/pair, 2015–2024). Leakage/future-invariance is pinned
by the Model F unit tests. Reproduce:

```bash
.venv/Scripts/python.exe scripts/build_daily_bars.py
.venv/Scripts/python.exe scripts/measure_momentum.py
```

## Evidence

### 1. Headline (D1, lookback 126) — no positive-same-side edge across pairs and periods

| pair | train net R | (gross t) | val net R | (gross t) |
|---|--:|--:|--:|--:|
| EUR/USD | +0.0504 | +0.86 | **−0.1151** | −0.73 |
| GBP/USD | +0.0074 | +0.25 | +0.0178 | +0.18 |
| USD/JPY | −0.0327 | −0.24 | **−0.1013** | −0.65 |

P4 needs positive net expectancy on the **same side**, in **train AND validation**, on **more than
one pair**. The headline clears none of it: EUR/USD is positive in train but **flips to −0.115 in
validation**; USD/JPY is negative in **both**; only GBP/USD is positive in both — at **t ≈ 0.2**,
i.e. indistinguishable from zero, and on a single pair. Every in-sample gross |t| < 0.9: at the daily
horizon on these three pairs the momentum sign is, to the available precision, **not** predictive.

### 2. Walk-forward (purged/embargoed sequential OOS blocks; no parameter is fit)

| pair / TF | net R (all blocks) | t | +50% stress | blocks net>0 |
|---|--:|--:|--:|:--:|
| EUR/USD **D1** | +0.0429 | +0.60 | +0.0352 | 2 / 5 |
| GBP/USD **D1** | +0.0185 | +0.26 | +0.0131 | 2 / 5 |
| USD/JPY **D1** | −0.0301 | −0.43 | −0.0374 | 2 / 5 |
| EUR/USD H4 | −0.0518 | −1.67 | −0.0699 | 1 / 5 |
| GBP/USD H4 | −0.0436 | −1.43 | −0.0565 | 1 / 5 |
| USD/JPY H4 | −0.0344 | −1.11 | −0.0519 | 1 / 5 |

On the headline **D1** timeframe the three pairs net to +0.043 / +0.019 / −0.030 R/trade, every
**|t| < 0.7**, each only **2 of 5** OOS blocks positive — a net coin-flip, not an edge. On the H4
context timeframe every pair is net-**negative** (t up to −1.7, 1/5 blocks). The per-block detail is
textbook trend-following regime dependence rather than a persistent edge: e.g. EUR/USD D1 earns
**+0.303 (t +1.82)** in block 4 (the 2021–2022 EUR downtrend) and gives **−0.216 (t −1.44)** back in
block 5 (the 2022–2023 reversal). Positive in sustained trends, negative in ranges/turns, ≈ 0 over
the 9-year span — exactly what an unconditional trend-follower does, and why one needs many
uncorrelated markets (not three majors) for its thin premium to show through the noise.

### 3. The structural prior was *half* right — and it still isn't enough

The cost-survival prediction **held**: D1 per-trade cost drag is ~**0.008–0.016 R** (H4 ~0.025–0.038
R), versus Model E's ~0.075 R (H1) / ~0.17 R (M15) — a **~5–20× reduction**, exactly as predicted by
the larger daily ATR stop. So Model F cleared the cost hurdle that killed E. But it fails anyway,
because **there is no gross directional edge for the low cost to protect**: the headline gross is
+0.066 / +0.018 / −0.017 (train) and −0.102 / +0.028 / −0.093 (val), all |t| < 0.9. This is a
*cleaner* failure than E's: E lost a near-zero gross edge to costs; F is a near-zero gross edge that
survives costs and is still near-zero. The prior correctly identified the cost mechanism and was
wrong that a daily momentum signal carries exploitable directional information on this pair set.

### 4. The recurring USD/JPY-2022/23 footprint reappears — in a *context* cell, not the headline

The headline (D1 lb126) is **negative** on USD/JPY val (−0.101), so it does **not** reproduce the
USD/JPY-2022/23 pop seen for Models D and E. But the *shorter* lookback context cell does:
**F mom63** on USD/JPY is +0.115 net on val D1 (t +0.89) and +0.104 on val H4 (t +2.06) — while being
**−0.120 (t −2.67)** on train H4, an outright sign flip across timeframe on the same pair. So the
2022–2023 yen decline is again caught by *a* short-horizon trend-follower (consistent with the
"USD/JPY intraday/short-trend regime" observation of ADR 0002/0003), it is internally contradictory
across timeframes, and it is **not** the pre-registered headline. It does not rescue Model F; it
reinforces that the only edge-like signals in this whole program keep pointing at one pair in one
period.

## Decision

**Model F fails P4 and is rejected.** The headline is net-negative or noise across pairs and periods,
its walk-forward is a |t| < 0.7 coin-flip on D1 and negative on H4, and the two context lookbacks
disagree with the headline and with each other (mom252 is in fact strongly *anti*-predictive OOS on
val D1: −0.32/−0.33/−0.28, t −2.18/−2.07/−1.75, on 68–79 trades). No cell offers a positive,
same-side, cross-pair, cross-period, stress-stable edge.

Consequences, per charter §1.5, §2, §3:

- **Classification (charter §2):** Model F as a standalone edge → **INVALID** (no stable gross edge;
  headline net-negative/flat OOS; walk-forward insignificant on D1 and negative on H4; lookbacks
  mutually inconsistent). The favourable *cost* behaviour is a validated mechanical fact, not an edge.
  The F-mom63 USD/JPY-val cells → **WEAK observation**, the same USD/JPY-2022/23 footprint, never a
  target (charter §1.3). Nothing is promoted.
- **No ML.** Baseline-before-ML needs a baseline edge to filter; there is none. No ML before a P4 GO.
- **Test window stays sealed.** Nothing on train/validation clears the bar.
- **A failed gate is a legitimate finding.** Six mechanisms — four SMC (A–D), one intraday non-SMC
  (E), and one daily-horizon non-SMC (F) — now fail P4 under identical, honest, leakage-controlled
  measurement. That is a real result about these three pairs at these horizons/costs.

### What this rules out (and what it does not)

Model F specifically tests **unconditional** time-series momentum on **three FX majors** through a
fixed ATR triple barrier. Its failure is consistent with the published evidence, which finds the
TSMOM premium **thin per-market** and reliant on **diversification across many (dozens of)
uncorrelated instruments** and on **volatility-scaled position sizing / signal-driven exits** — none
of which three majors and a fixed barrier provide. So this result does *not* refute TSMOM in general;
it shows that the tradeable, robust form of it is **out of reach of this platform's current universe
and single-instrument, fixed-barrier engine**. Pursuing it properly would mean a genuinely different
build (a broad multi-asset cross-section, vol-scaling, portfolio-level accounting), which is a much
larger effort and a separate decision — not another single-pair setup.

## Where this leaves the research

The pre-ML rule-baseline conclusion of ADR 0002/0003 stands and is reinforced: **hold at P4, no ML,
test sealed.** The plan's SMC universe (A–D) plus two genuinely new, structurally-motivated
mechanisms (E intraday volatility; F daily momentum) are all rejected. The one recurring edge-like
signal across the entire program remains the **USD/JPY 2022–2023** window (Models D, E, and now the
F-mom63 context cell), which points at a *pair/period regime*, not a setup edge, and may **not** be
validated on the sealed test window. Any further mechanism must clear the same bar of a **prior
structural reason** to expect an edge; the momentum prior was the strongest such reason available for
a single-instrument engine and it did not deliver, which is itself informative about how much further
ad-hoc, single-pair rule-search is worth.

No numbers in this record are fabricated; all trace to `scripts/measure_momentum.py`,
`scripts/build_daily_bars.py`, and `experiments/registry.jsonl`.
