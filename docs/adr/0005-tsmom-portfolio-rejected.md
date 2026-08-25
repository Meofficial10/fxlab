# ADR 0005 — TSMOM portfolio (multi-asset, vol-scaled, return-space) rejected at P4

- **Status:** Accepted (2026-08-25). Extends [ADR 0001](0001-p4-no-go-smc-rule-baseline.md),
  [ADR 0002](0002-usdjpy-h4-model-d-lead-investigated.md),
  [ADR 0003](0003-model-e-session-breakout-rejected.md), and
  [ADR 0004](0004-model-f-momentum-rejected.md) — P4 remains **NO-GO**.
- **Phase gate:** P4 Statistical testing (charter [§3](../00-charter.md#3-phase-gates))
- **Decision:** **P4-candidate mechanism #7 — multi-asset time-series momentum in PORTFOLIO form**
  (vol-scaled, return-space, 14-instrument cross-asset panel), chosen specifically to address Model
  F's structural limitation (single-instrument, three correlated FX majors), was built and measured
  with the identical discipline and held to the identical P4 gate. It **fails P4** — the validation
  Sharpe is positive but **sub-period fragile** (only 2 of 6 sequential OOS blocks positive, below
  the 4/6 robustness threshold). **Reject it.** No ML is built; the test window (2024+) **stays sealed**.

## Context

After Model F (daily time-series momentum on three FX majors) failed P4 (ADR 0004), the postmortem
noted that F's failure was **consistent with** the literature (Moskowitz–Ooi–Pedersen 2012; Hurst–
Ooi–Pedersen), which finds the TSMOM premium *thin per-market* and reliant on (a) **diversification
across many (dozens of) low-correlation instruments**, (b) **volatility-scaled position sizing**, and
(c) **portfolio-level accounting** — none of which the single-instrument, three-correlated-FX-major,
fixed-ATR-barrier engine provides. ADR 0004 explicitly stated that this result "does *not* refute
TSMOM in general; it shows that the tradeable, robust form of it is **out of reach of this platform's
current universe and single-instrument engine**. Pursuing it properly would mean a genuinely different
build (a broad multi-asset cross-section, vol-scaling, portfolio-level accounting)."

**This ADR documents exactly that genuinely-different build** — P4-candidate #7 was constructed
specifically to test whether the structurally-motivated, literature-prescribed form of TSMOM survives
when built correctly.

**Structural prior (stated before any measurement).** A diversified portfolio that, at each monthly
rebalance, holds each instrument long/short by the sign of its trailing 12-month return, sized
inversely to that instrument's own recent volatility (equal ex-ante risk per instrument, constant
ex-ante portfolio-vol target), earns a **positive Sharpe net of realistic costs** — robust across
sub-periods and asset classes and stable under a +50% cost stress. The **specific structural reason**
it may succeed where Models A–F failed is the **diversification across many low-correlation return
streams** (7 FX majors + 2 metals + 2 energy + 3 equity indices, not three correlated pairs).

**Frozen hypothesis (UNPROVEN — measured, never tuned toward):** the above.

The **pre-registered headline** is exactly one cell-family: **lookback = 252 (~12 months, canonical
MOP), vol_window = 60, rebalance = 21 (monthly), target_ann_vol = 10%, weight_cap = 4.0** — the
literature-standard configuration fixed *a priori*. Two context configurations (lookback 63, and
weekly rebalance) were declared up front purely as robustness context (reported, **not judged**).

All numbers are RESULTs on **real Dukascopy** daily data, train (≤2021-12-31) + validation
(2022–2023) only; the test window (2024+) is **sealed at load** — each instrument is truncated to
`<= val_end` so the driver can never read a 2024+ bar. D1 bars are a deterministic resample of the
stored H1 (`scripts/build_daily_bars.py`). A dedicated **panel backtester**
(`src/fxlab/backtest/panel.py`) and **portfolio metrics** (`src/fxlab/backtest/portfolio_metrics.py`)
were built for this — return-space, vol-scaled, ragged-panel machinery that is deliberately separate
from the single-symbol event engine (which is one-position-at-a-time, ATR-triple-barrier, R-multiple
by construction — the wrong shape). Leakage/future-invariance is pinned by 11 unit tests in
`tests/test_panel_backtest.py`. The full test suite (155 tests) passes; `ruff` is clean. Reproduce:

```bash
.venv/Scripts/python.exe scripts/build_tsmom_universe.py   # 14 instruments kept (all real)
.venv/Scripts/python.exe scripts/measure_tsmom_portfolio.py
```

## Evidence

### 1. The 14-instrument cross-asset panel (real Dukascopy, all ≤ 2023-12-31)

| symbol | class | rows ≤ val | span |
|---|---|--:|---|
| AUDUSD | fx | 2,813 | 2015-01-01..2023-12-29 |
| BRENT | energy | 2,346 | 2015-01-02..2023-12-29 |
| EURUSD | fx | 2,813 | 2015-01-01..2023-12-29 |
| GBPUSD | fx | 2,812 | 2015-01-01..2023-12-29 |
| GER40 | index | 2,608 | 2015-01-01..2023-12-29 |
| NAS100 | index | 2,639 | 2015-01-02..2023-12-29 |
| NZDUSD | fx | 2,810 | 2015-01-01..2023-12-29 |
| SPX500 | index | 2,637 | 2015-01-02..2023-12-29 |
| USDCAD | fx | 2,813 | 2015-01-01..2023-12-29 |
| USDCHF | fx | 2,813 | 2015-01-01..2023-12-29 |
| USDJPY | fx | 2,813 | 2015-01-01..2023-12-29 |
| WTI | energy | 2,787 | 2015-01-02..2023-12-29 |
| XAGUSD | metal | 2,795 | 2015-01-01..2023-12-29 |
| XAUUSD | metal | 2,795 | 2015-01-01..2023-12-29 |

**panel:** 14 instruments | by class: fx=7, metal=2, energy=2, index=3

The universe was built by `scripts/build_tsmom_universe.py` with documented liquidity/cost criteria
(liquid CFDs/spot; cost < 5 bp one-way; real Dukascopy availability). All 14 passed and are kept —
no fabricated symbols, no survivorship filtering.

### 2. Headline configuration (the ONLY cell judged vs P4)

| split | mode | days | ann ret % | vol % | Sharpe | Shrp_gr | Sortino | maxDD % | turn/y | cost % |
|---|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| **train** | normal | 1,936 | **−2.15** | 16.75 | **−0.05** | −0.03 | −0.06 | 48.7 | 16.27 | 0.27 |
| **train** | stress | 1,936 | −2.29 | 16.75 | −0.05 | −0.03 | −0.07 | 49.0 | 16.27 | 0.40 |
| **val** | normal | 624 | **+5.75** | 19.16 | **+0.39** | +0.40 | +0.55 | 32.3 | 13.43 | 0.21 |
| **val** | stress | 624 | +5.65 | 19.16 | +0.38 | +0.40 | +0.54 | 32.4 | 13.43 | 0.31 |

- **Validation Sharpe:** +0.39 net of costs (normal), +0.38 under +50% cost stress — both **positive** ✅
- **Train Sharpe:** −0.05 (essentially zero; negative sign is in-sample noise).
- The **cost model** works as designed: cost drag ~0.21–0.27% annualized (realistic bps turnover costs
  in return space), and the strategy survives +50% stress (val Sharpe +0.39 → +0.38).

### 3. Sequential OOS sub-period blocks (headline, train+val timeline, nothing fit)

| block | from | to | days | ann ret % | Sharpe |
|--:|---|---|--:|--:|--:|
| 1 | 2015-10-23 | 2017-03-03 | 426 | −12.38 | **−0.80** |
| 2 | 2017-03-05 | 2018-07-16 | 427 | −8.00 | **−0.41** |
| 3 | 2018-07-17 | 2019-11-26 | 427 | −1.11 | +0.00 |
| 4 | 2019-11-27 | 2021-04-07 | 426 | +10.67 | **+0.64** |
| 5 | 2021-04-08 | 2022-08-18 | 427 | +14.78 | **+0.82** |
| 6 | 2022-08-19 | 2023-12-29 | 427 | −2.92 | −0.06 |

**Blocks with net ann return > 0:** **2 / 6** (blocks 4 and 5 only)  
**Strict-majority bar (P4 robustness threshold):** ≥ 4 / 6

The positive validation Sharpe is **concentrated in blocks 4 and 5** (2019–2022), which overlap the
train/val boundary. Blocks 1, 2, 3, and 6 are flat-to-negative. The portfolio earned during a
specific favourable window (the 2020–2022 post-COVID trends + yen/energy moves) and gave much of it
back (block 6 is the 2022–2023 reversal period). This is **not** a stable, cross-period edge — it is
**regime-dependent** performance, exactly what unconditional TSMOM does in the absence of enough
uncorrelated markets or a regime filter.

### 4. Drop-one-asset-class (headline, validation, net of costs)

| removed class | kept instruments | classes left | val Sharpe | val ann ret % |
|---|--:|--:|--:|--:|
| fx | 7 | 3 | +0.11 | +0.54 |
| metal | 12 | 3 | +0.45 | +6.71 |
| energy | 12 | 3 | +0.36 | +5.38 |
| index | 11 | 3 | +0.21 | +2.15 |

Every drop-one-class validation Sharpe is **positive** ✅ — the result does **not** hang on a single
asset class. The FX-dropped portfolio is the weakest (+0.11), which makes sense: FX is the largest
class (7 of 14) and carries the lowest idiosyncratic vol (currencies co-move more than
metals/energy/indices).

### 5. Context configurations (reported, NOT judged)

**ctx:lb63** (3-month lookback, monthly rebalance):
- val normal: +7.38% ann ret, Sharpe +0.46 (stress +0.45)
- Higher turnover (20.67/year vs headline 13.43), higher stress-drag

**ctx:weekly** (12-month lookback, weekly rebalance):
- val normal: −0.36% ann ret, Sharpe +0.07 (stress +0.07)
- Much higher turnover (23.21/year), stress-drag kills it

The shorter lookback (lb63) is more responsive and caught the 2022–2023 validation period better
(Sharpe +0.46 vs headline +0.39), but at the cost of higher turnover and more stress-drag. The weekly
rebalance (higher turnover, same lookback) is near-zero net. These are exactly the turnover/signal-
responsiveness tradeoffs the literature documents. They do **not** rescue the headline; the headline
is the **only** cell judged.

## Mechanical P4 checklist (pre-committed, headline only, train+val)

- ✅ **val net Sharpe > 0:** +0.39
- ✅ **val net Sharpe > 0 under +50% stress:** +0.38
- ❌ **sub-period robust (≥ 4/6 blocks net>0):** **FAILS** — only 2/6 positive
- ✅ **not single-class-dependent (every drop-one-class val Sharpe > 0):** all positive

**All mechanical checks pass:** **False**

## Decision

**P4-candidate #7 (TSMOM portfolio) fails P4 and is rejected.** The validation Sharpe is positive and
stress-stable, but it is **sub-period fragile**: only 2 of 6 sequential OOS blocks show positive
returns, far below the required 4/6 threshold. The positive validation result is concentrated in
blocks 4 and 5 (2019–2022), which captured the post-COVID trends and the 2022 yen/energy moves, while
blocks 1, 2, 3, and 6 (the 2015–2018 range and the 2022–2023 reversal) are flat-to-negative. This is
**regime-dependent** performance, not a stable cross-period edge.

Consequences, per charter §1.5, §2, §3:

- **Classification (charter §2):** TSMOM portfolio as a standalone edge → **WEAK** (positive OOS
  Sharpe, stress-stable, not single-class-dependent, but **sub-period fragile** — concentrated in a
  favourable 2019–2022 window, not robust across the full 9-year span). It is an observation to
  investigate, **not** a validated edge. Nothing is promoted to VALID.
- **No ML.** Baseline-before-ML needs a baseline edge to filter; there is none that clears the
  robustness bar. No ML before a P4 GO.
- **Test window stays sealed.** Nothing on train/validation clears the full P4 gate.
- **A failed gate is a legitimate finding.** Seven mechanisms — four SMC (A–D), two intraday non-SMC
  (E, F on H1/H4), one daily non-SMC (F on D1), and now one **multi-asset portfolio-form** non-SMC
  (TSMOM) — fail P4 under identical, honest, leakage-controlled measurement. That is a real result
  about these instruments at these horizons/costs/universe-size.

### What this rules out (and what it does not)

This result specifically tests **unconditional** 12-month time-series momentum on a **14-instrument**
cross-asset panel with **diagonal vol-targeting** (zero-correlation assumption) and **no regime
filter**. Its failure to meet the strict-majority sub-period robustness bar (4/6) is consistent with
the literature's findings that TSMOM's premium is **thin** and requires (a) **many more instruments**
(dozens, not 14) to diversify the idiosyncratic noise, and/or (b) **regime awareness** (e.g., a
volatility-regime filter, or a risk-parity overlay with fitted correlations, neither of which this
diagonal/unconditional build provides).

So this result does *not* refute TSMOM in general; it shows that even the portfolio-correct form is
**marginal** on a 14-instrument universe without regime conditioning, and that **2 of 6 positive
sub-periods is insufficient** for the P4 robustness standard this platform demands. Pursuing it
further would mean (a) a much larger universe (50+ instruments, more data/maintenance), (b) a regime
filter or risk-parity overlay (more complexity, more parameters, more overfitting risk), or (c)
accepting a WEAK result and living with the regime dependence. None of those are trivial decisions.

## Where this leaves the research

The pre-ML rule-baseline conclusion of ADRs 0001–0004 stands and is **definitively reinforced**:
**hold at P4, no ML, test sealed.** The plan's SMC universe (A–D), two intraday non-SMC mechanisms
(E, F-intraday), one daily single-instrument non-SMC (F-daily), and now one **multi-asset
portfolio-form** non-SMC (TSMOM) are all rejected or classified WEAK. The single recurring edge-like
signal across the entire program remains the **USD/JPY 2022–2023** window (Models D, E, F-mom63) +
the **2019–2022 post-COVID/yen/energy window** (TSMOM blocks 4–5), both pointing at **favourable
regimes**, not setup edges, and neither validated on the sealed test window.

**The structural prior was correct** (diversification + vol-scaling + portfolio accounting **is** the
right way to test TSMOM, and the machinery built here is honest and reusable), but the **universe size
+ horizon + lack of regime conditioning** left the premium too thin and too regime-dependent to meet
this platform's **strict-majority sub-period robustness bar**. That bar (4/6 blocks) is a choice; a
weaker bar (e.g., 3/6, or "positive mean across blocks") would pass this result. But the charter
chooses to demand strict-majority robustness exactly to **not** build on regime-dependent signals, and
this result respects that choice.

Any further mechanism must clear the same bar of a **prior structural reason** to expect an edge. The
TSMOM prior was the strongest such reason available for a momentum/trend-following strategy; it
delivered a WEAK result (positive OOS, but sub-period fragile), which is **more** than Models A–F
delivered (all INVALID or single-pair WEAK), yet still **not enough** for a P4 GO under this
platform's robustness standard.

No numbers in this record are fabricated; all trace to `scripts/measure_tsmom_portfolio.py`,
`scripts/build_tsmom_universe.py`, `src/fxlab/backtest/panel.py`,
`src/fxlab/backtest/portfolio_metrics.py`, and `experiments/registry.jsonl`.
