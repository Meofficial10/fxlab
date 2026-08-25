# ADR 0001 — P4 is NO-GO for the SMC rule baseline (Models A–D, EUR/USD + GBP/USD + USD/JPY)

- **Status:** Accepted (2026-08-24)
- **Phase gate:** P4 Statistical testing (charter [§3](../00-charter.md#3-phase-gates))
- **Decision:** **NO-GO.** Do not proceed to P5 (the AI filter). No ML is built. The test
  window (2024+) **stays sealed** — nothing measured here justifies spending it.

## Context

The charter's P4 gate requires **positive expectancy net of costs on the untouched test set,
robust across ≥2 pairs and ≥2 periods, stable under +50% cost stress**. Before spending the test
window, an edge must at least show up net-of-costs, on the *same side*, in the train and
validation splits of more than one pair. This ADR records the terminal RESULT of the pre-ML
research: the plan's entire setup universe measured across all three plan pairs.

All numbers are RESULTs — produced by code in this repo on **real Dukascopy data**
(EUR/USD, GBP/USD, USD/JPY, H1 + H4, 2015→2024), reproducible from the experiment registry.
Splits: train ≤ 2021-12-31, validation 2022–2023 (both fully covered); test 2024+ sealed.
Reproduce with:

```bash
.venv/Scripts/python.exe scripts/measure_cost_to_edge.py    # EUR/USD filter grid (P3.1–P3.3)
.venv/Scripts/python.exe scripts/measure_cross_pair.py      # 3-pair core-mechanism grid (P3.4)
```

`expR` = mean R per trade (ATR units, unit-agnostic across pairs); `t` = in-sample t of gross R
vs 0 (uncorrected). USD/JPY's 0.01 pip size is handled by the per-pair cost config, so net
figures are comparable.

## Evidence

The setup universe (charter §2 objects, all born UNPROVEN):

- **Model A** — liquidity-sweep reversal (fade a single-bar wick sweep of a swing).
- **Model B** — trend-pullback (EMA), the deliberately naive baseline.
- **Model C** — breakout-failure / fakeout reversal (fade a *trapped* close-based breakout).
- **Model D** — FVG-retracement continuation (enter continuation on a tap back into an
  unfilled fair-value gap).

### 1. Model A's gross edge is EUR/USD-specific — it does not replicate

The only in-sample *gross* signal the program ever produced was Model A on EUR/USD
(+0.042 R/trade, t 1.65, H1 train). On the two robustness pairs it is **negative gross in
every cell**, significantly so out of sample:

| pair | A base val H1 gross (t) | A base val H4 gross |
|---|---|---|
| EUR/USD | +0.016 (0.34) | +0.006 |
| GBP/USD | **−0.096 (−2.15)** | −0.014 |
| USD/JPY | **−0.084 (−1.80)** | −0.067 |

A setup whose only positive cell is on one pair, and which loses gross on the other two majors,
is the opposite of robust. Net of costs it loses in every cell on all three pairs.

### 2. Confirmation filters and Models B, C sign-flip — no stable side

`A +structure` (the P3.1 in-sample "lift") flips sign across pairs/periods (GBP train H4
+0.099 → val H4 −0.124; USD/JPY val H1 −0.147, t −2.12). Model C stays small-negative
in-sample with mixed, sub-significant OOS cells that same-pair train contradicts. The **naive
baseline** Model B itself throws sign-*flipping* |t|>2 cells on one pair/TF (GBP/USD H1: train
+0.076, t **+2.23** → val −0.119, t **−2.09**) — a direct demonstration that single-cell
|t|≈2 here is noise, not edge.

### 3. Model D — one sub-significant, single-pair lead (classified WEAK)

Model D is the one mechanism with a mildly *positive* gross lean off EUR/USD: positive gross in
all four USD/JPY cells and three of four GBP/USD cells. On **USD/JPY H4** it is net-positive on
the same (long-continuation) side in **both** splits and survives +50% stress:

| USD/JPY H4 | trades | gross (t) | net | net +50% | PF gross |
|---|--:|--:|--:|--:|--:|
| train | 1110 | +0.066 (1.54) | +0.029 | +0.010 | 1.103 |
| val | 300 | +0.156 (1.86) | +0.133 | +0.122 | 1.256 |

This is the single most edge-like result the program has produced. It nonetheless **fails the
P4 gate**: it holds on only **one pair** (EUR/USD H4 Model D is net −0.152 OOS; GBP/USD H4 is
~breakeven and negative under stress), it is net-negative on H1 everywhere (cost drag ≈ gross),
and its t-stats are <2 in-sample — unremarkable against the ~60 cells this survey spans. Per
charter §1.3 it is **an observation to investigate later, never a target to tune toward.**

## Decision

**P4 = NO-GO.** No setup shows positive expectancy net of costs on the same side across ≥2 pairs
and ≥2 periods under stress. Consequences, per charter §1.5 and §3:

- **No ML.** P5 does not begin; baseline-before-ML means there is no baseline edge to filter.
- **Test window stays sealed.** There is nothing worth spending it on.
- **Classifications (charter §2):** Model A sweep reversal → **INVALID** as a standalone edge
  (pair-specific gross, net-negative everywhere); Model C → **INVALID** (no edge, mildly
  anti-predictive in-sample); Model D → **WEAK** (one net-positive, stress-robust pair/TF cell,
  not robust across pairs, sub-significant); the confirmation filters → **WEAK** (in-sample
  selection effects that reverse OOS). Model B remains the no-edge baseline it was designed as.

This is a **successful research outcome**, not a failure: the platform's core job is to reach a
disciplined, honest "no robust edge here" and stop — which is exactly what it did.

## Options from here (all pre-ML; none is a P4 pass)

1. **Conclude the rule-baseline research** and hold at P4. Charter-aligned default — "most of
   the value of this platform is its willingness to stop at P4."
2. **Investigate the USD/JPY-H4 Model D lead** as an explicitly WEAK, exploratory thread (why
   JPY? why H4 only? is it a carry/session/volatility artifact?). It cannot clear "≥2 pairs" by
   construction, so it is research, not a P4 candidate, and must not be tuned toward.
3. **A genuinely new mechanism** outside the A–D universe (e.g. session-timing, range breakout,
   volatility mean-reversion), tested with identical discipline. Extends the search; no prior
   reason to expect it clears P4 where four SMC mechanisms did not.

No numbers in this record are fabricated; all trace to `experiments/registry.jsonl`.
