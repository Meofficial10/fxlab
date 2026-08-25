# fxlab Charter — the rules the whole system obeys

> This is a **research and validation platform first, and a trading system last.**
> Its job is to find out whether a real, tradeable edge exists — and to say so
> honestly when it does not. A clean "no edge here" is a successful result.

This document is the refined, deduplicated form of the original 33-section master
prompt. Everything downstream (code, configs, experiments, reviews) is bound by it.

---

## 1. Prime directives

1. **Never fabricate numbers.** No invented win rates, profits, backtest results,
   Sharpe ratios, or equity curves — ever. If a number isn't computed from data by
   code in this repo, it does not get stated. Placeholders are labelled as such.
2. **Optimize expectancy and robustness net of costs — not win rate.** A high hit-rate
   is trivially manufactured with rare entries and terrible reward:risk. The objective
   function is **expected value per trade after spread, commission, slippage, and
   latency**, and whether that value *survives* out-of-sample, across pairs, across
   periods, and under cost stress.
3. **The "80–90% accuracy" figure is an observation to explain, never a target to hit.**
   If a setup ever shows such a hit-rate, we treat it as a *phenomenon to investigate*
   (is it survivorship? rare-event framing? look-ahead? favourable period?) and we
   report the accompanying reward:risk and expectancy. We do **not** tune toward it.
   Tuning toward accuracy is how you build a confident, broke system.
4. **No look-ahead, no leakage.** Features read only closed candles. Labels never leak
   into features. Scalers fit on train only. Any run that trips a leakage test is
   discarded (§4).
5. **Baseline before ML. Simple before complex.** A measurable rule-based baseline must
   exist and be beaten out-of-sample before any model is allowed in; a complex model
   must beat a simple one out-of-sample or it is cut.
6. **The AI never bypasses the risk engine.** Position sizing and kill-switches are hard
   constraints. No model output can override them.

---

## 2. Evidence vocabulary (used in every claim, commit, and report)

Label every empirical statement:

- **HYPOTHESIS** — a proposed idea, untested. Costs nothing to write down.
- **RESULT** — a number produced by code on data, with the config/data hash that made it.
- **VALIDATED** — a RESULT that held on the **untouched** test set under the §7 gate.

Classify every idea/technique:

| Class | Meaning |
|---|---|
| **VALID** | Demonstrated out-of-sample, net of costs, robust. |
| **PLAUSIBLE** | Reasonable, partial or in-sample support; not yet OOS-confirmed. |
| **UNPROVEN** | Stated but not yet tested in this repo. |
| **WEAK** | Tested; effect is small, fragile, or period-dependent. |
| **INVALID** | Tested and failed, or depends on leakage/overfitting. |

The dukascopy adapter, every SMC detector, and every setup start life **UNPROVEN**.

---

## 3. Phase gates (a phase cannot start until the previous gate passes)

| Phase | Objective | Gate |
|---|---|---|
| **P1 Foundation** | Data contract, ingest, validation, resampling, cost model, splits, leakage harness | Reproducible clean ingest passing all schema checks; cost model documented; purge/embargo splits implemented; **future-invariance leakage test green**; `pytest` green. |
| **P2 Rule baseline** | ≥1 setup as objective rules, backtested net of costs | Fully-specified reproducible rules + full metric set (win rate, avg win/loss, **expectancy**, PF, DD, streaks) net of costs, logged as an experiment. No edge required yet — only honesty + leakage-safety. |
| **P3 SMC layer** | Each SMC concept a unit-tested detector | Fixtures + tests per detector; incremental expectancy vs P2 **measured** (may be negative). |
| **P4 Statistical testing** ⭐ | Decide if an edge is real | **GO/NO-GO:** positive expectancy net of costs on the **untouched test set**, robust across **≥2 pairs and ≥2 periods**, stable under **+50% cost stress**. Else: do **not** build AI. |
| **P5 AI filter** | Calibrated `P(TP before SL)` filtering P4 setups | Must **improve OOS expectancy vs the unfiltered baseline**, with good calibration (reliability curve, Brier), purged CV, no leakage. Else: cut the model. |
| **P6 Risk engine** | Sizing + kill-switches integrated | Risk-of-ruin under a preset ceiling on realistic streaks; **martingale demonstrably rejected** by the ruin sim. |
| **P7 Walk-forward** | No single-period dependence | Holds across rolling OOS windows; parameter stability shown. |
| **P8 Paper trading** | Live-data sim, no money | Paper ≈ backtest within tolerance over preset N trades; latency/exec assumptions validated. |
| **P9 Small live** | Tiny predefined risk | Live within statistical tolerance of paper; zero risk-limit breaches. |
| **P10 Scaling** | Grow only on evidence | Statistically meaningful live edge; sizing scaled gradually with continuous validation. |

P4 is the hinge. Most of the value of this platform is its willingness to stop at P4.

---

## 4. Leakage doctrine (enforced in code, not prose)

- **Closed-candle-only** features (point-in-time contract): a feature at bar `t` may
  touch only bars `≤ t`. Guarded by a **future-invariance regression test** — append
  arbitrary future bars, assert every past output is byte-identical
  (`tests/test_leakage_guards.py`).
- **Right-labelled resampling**; signals act on the **next** bar (latency ≥ 1 bar).
- **Multi-timeframe alignment** attaches only the most recent **closed** HTF bar
  (backward `merge_asof` on HTF close time). The currently-forming HTF bar is excluded.
- **Ties → SL first; gaps → fill at open.** Conservative by default.
- **Purge + embargo** at every train/test boundary so label windows can't straddle it.
- **Scalers/normalizers fit on train folds only.**
- A run that trips any leakage test is **discarded**.

## 5. Cost doctrine

Every backtest reports **gross AND net**. Costs = spread + commission + slippage(volatility)
+ latency. A **+50% stress** run (spread & slippage) is part of the P4 gate. Costs are
modelled such that they can only ever *reduce* net PnL (property-tested).

## 6. Risk doctrine

Hard caps: max risk/trade, max daily loss, max consecutive losses, max drawdown,
max trades/day, exposure/correlation caps. Sizing schemes are pluggable
(fixed-fractional, anti-martingale, conditional-progressive by setup grade). Martingale
is included **only to be demonstrated as ruinous**. The `$10` experiment and risk-of-ruin
Monte-Carlo report ending-balance percentiles, max DD, longest losing streak, and
**P(ruin)** with tails shown, never hidden.

## 7. What "done" means for a claim

A claim is **VALIDATED** only when it is a RESULT that (a) came from code in this repo,
(b) is reproducible from a logged config + data hash, (c) held on the untouched test set,
and (d) survived the robustness checks of the relevant phase gate. Anything less keeps
its lower evidence label.
