# ADR 0002 — USD/JPY-H4 Model D lead investigated; pre-ML rule-baseline research concluded at P4

- **Status:** Accepted (2026-08-24). Extends [ADR 0001](0001-p4-no-go-smc-rule-baseline.md) — does not supersede it; P4 remains NO-GO.
- **Phase gate:** P4 Statistical testing (charter [§3](../00-charter.md#3-phase-gates))
- **Decision:** The one edge-like cell — **Model D on USD/JPY H4** — was investigated as an explicitly
  WEAK, exploratory thread (ADR 0001, option 2). It **does not survive** as a robust edge. The
  rule-based SMC baseline (Models A–D) has **no robust edge**. **Conclude the pre-ML research and
  hold at P4. No ML.** The test window (2024+) **stays sealed**.

## Context

ADR 0001 recorded P4 = NO-GO and left three honest options. This ADR resolves **option 2**:
investigate *why* the USD/JPY-H4 Model D cell looked edge-like, and report whether the lead
survives non-overfit decomposition — **without** tuning toward it, declaring a P4 pass, or touching
the sealed test window.

The a-priori suspicion was **directional beta**: Model D is a *continuation* (trend-following)
setup, so a plausible dull explanation is that its USD/JPY net edge was just riding the 2021–2023
yen decline, which EUR/USD lacked. That hypothesis was tested and **refuted** (below).

All numbers are RESULTs on **real Dukascopy** data, train (≤2021-12-31) + validation (2022–2023)
only; the test window is never read. Reproduce (deterministic, `seed=12345`):

```bash
.venv/Scripts/python.exe scripts/investigate_jpy_d.py
```

Method (three non-overfit decompositions, nothing fitted):
1. **Side decomposition** — long vs short net expectancy per split.
2. **Directional benchmark** — always-long / always-short through the same one-position engine, to
   measure the ambient per-trade drift each side earns with *no* FVG logic.
3. **Random-timing placebo** — 1000 signal sets with Model D's exact long/short counts but at
   random entry times; Model D's percentile in that null isolates whether its *timing* adds
   anything beyond the side mix. EUR/USD H4 (where Model D fails) is run alongside as a contrast.

## Evidence

### 1. The trend-beta explanation is REFUTED

| USD/JPY H4 | close drift | Model D net | long net | short net | always-long | always-short |
|---|--:|--:|--:|--:|--:|--:|
| train | −4.0% | +0.0287 | −0.0421 | **+0.1019** | −0.0561 | −0.0274 |
| val | +22.4% | +0.1334 | +0.1310 | **+0.1368** | +0.0721 | −0.1490 |

- On **train**, USD/JPY drifted **−4.0%** (flat-to-down), yet Model D was net-positive — and the
  positive contribution is on the **short** side (+0.1019), which *also* beat the always-short
  benchmark (−0.0274). Beta to a flat/falling market cannot come from a profitable short book.
- On **val**, USD/JPY rose **+22.4%**, yet Model D's **shorts made +0.1368** while blindly
  always-short lost **−0.1490**. A short book that is profitable in a 22% uptrend is the *opposite*
  of directional beta. The always-long / always-short benchmarks do not explain Model D either split.

So the dull explanation is dead. The persistent thread is instead a **short-biased anomaly on
USD/JPY H4**: short-side net is positive in **all three** train sub-periods and on val, while the
long side is negative across train and only rescued on val by the drift.

| USD/JPY H4 train, 3 equal-count chunks | net | long | short |
|---|--:|--:|--:|
| 2015-01 .. 2017-05 (370 tk) | +0.0039 | −0.0791 | +0.0878 |
| 2017-05 .. 2019-09 (370 tk) | +0.0876 | +0.0005 | +0.1729 |
| 2019-09 .. 2021-12 (370 tk) | −0.0054 | −0.0469 | +0.0409 |

The full-train *magnitude* leans on the middle chunk, but the short-side *sign* is consistent
across all three windows — the one genuinely stable feature found.

### 2. Above-random timing on the lead pair — but marginal, and only there

Random-timing placebo (matched side counts, 1000 draws, seed 12345):

| cell | Model D net | null mean ± sd | percentile | z |
|---|--:|--:|--:|--:|
| USD/JPY H4 train | +0.0287 | −0.0410 ± 0.0447 | 94.0th | +1.56 |
| USD/JPY H4 val | +0.1334 | +0.0263 ± 0.0828 | 89.7th | +1.29 |
| EUR/USD H4 train | −0.0418 | −0.0639 ± 0.0446 | 68.3th | +0.50 |
| EUR/USD H4 val | −0.1517 | −0.0156 ± 0.0838 | **5.0th** | **−1.62** |

On USD/JPY H4 the entry timing beats ~90% of same-side-mix random portfolios — a real but **sub-2σ**
signal (consistent with the <2 in-sample gross t-stats, and unremarkable across the ~60 cells this
survey spans). **Caveat:** the placebo randomizes side assignment across random times, so it cannot
separate genuine FVG-*geometry* timing from generic momentum state-conditioning (being short after a
down-impulse). It shows "better than random same-side entries," not "the FVG shape is the source."

On EUR/USD H4 val, Model D sits at the **5th percentile** of its own null (z −1.62) — i.e. *worse*
than random. The effect holds on exactly one pair, and is actively anti-predictive on a second.

## Decision

**The lead does not survive as a robust edge**, and P4 remains **NO-GO**:

- It clears at most **one pair** (P4 requires ≥2); on EUR/USD it is anti-predictive, and GBP/USD H4
  is ~breakeven / fails stress (ADR 0001).
- Significance is **marginal** (z ≈ 1.3–1.6 vs the random null; in-sample gross t < 2).
- The source is a **short-biased, single-pair anomaly**, not the symmetric FVG-continuation
  mechanism the setup was built to express.

Consequences, per charter §1.5 and §3:

- **Conclude the pre-ML rule-baseline research and hold at P4.** The plan's setup universe (A–D) is
  exhausted across all three plan pairs; every honest thread (cost-to-edge lift, two independent
  mechanisms, cross-pair, and now this decomposition) is closed.
- **No ML.** Baseline-before-ML means there is no baseline edge to filter. No ML before a P4 GO.
- **Test window stays sealed.** Nothing measured on train/validation clears the ≥2-pair bar, so
  spending the untouched 2024+ window is unjustified.
- **Classification (charter §2):** Model D on USD/JPY H4 stays **WEAK** — the investigation
  *sharpened* the anomaly (it is not beta, and beats random timing on one pair) rather than
  dissolving it, so it is a legitimately interesting **observation to investigate later, never a
  target to tune toward** (charter §1.3). It is not promoted; it is documented and parked.

This is a **successful research outcome**: the platform did its core job — pursued the single most
edge-like result to an honest, non-overfit conclusion and stopped, rather than manufacturing a pass.

## If the thread is ever picked up again (research, not a P4 candidate)

Purely for the record — none of these is a P4 path, and none may be tuned toward on the sealed test:
a short-only USD/JPY study; testing whether the short lean is a **session / carry / roll** artifact;
a placebo that fixes entry *times* and randomizes only side (to isolate FVG geometry from momentum
state-conditioning); and replication on an independent USD/JPY data source before any belief.

No numbers in this record are fabricated; all trace to `scripts/investigate_jpy_d.py` (seed 12345)
and `experiments/registry.jsonl`.
