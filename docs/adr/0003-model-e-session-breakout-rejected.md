# ADR 0003 — Model E (session opening-range breakout, non-SMC) rejected at P4

- **Status:** Accepted (2026-08-24). Extends [ADR 0001](0001-p4-no-go-smc-rule-baseline.md) and [ADR 0002](0002-usdjpy-h4-model-d-lead-investigated.md) — P4 remains **NO-GO**.
- **Phase gate:** P4 Statistical testing (charter [§3](../00-charter.md#3-phase-gates))
- **Decision:** **Model E — session opening-range breakout**, the first mechanism deliberately
  *outside* the SMC family (Models A–D), was built and measured with the identical discipline and
  held to the identical P4 gate. It **fails P4** (no robust net edge across ≥2 pairs and ≥2 periods,
  worse under stress). **Reject it.** No ML is built; the test window (2024+) **stays sealed**.

## Context

After the SMC universe (A–D) was exhausted (ADR 0001/0002), the only remaining pre-ML avenue was a
**genuinely new mechanism outside A–D**. Model E is a classic, non-SMC, time-of-day *volatility*
idea, chosen precisely because it is mechanistically unrelated to the SMC reversal/continuation
setups. The previous SMC results were **not** used to justify, seed, or force it.

**Frozen hypothesis (UNPROVEN — measured, never tuned toward):** intraday FX has a time-of-day
volatility structure; when a major session opens (canonically **London, 07:00–16:00 UTC**),
participation/volatility rise and the level at which the session's first *range* breaks tends to mark
the direction flow commits to — so a breakout of the session's **opening range**, taken in the
breakout direction, *may* carry positive expectancy net of costs.

The **pre-registered headline** test of that hypothesis is exactly one cell-family: **London,
`or_bars=1`**. Two further configs were declared up front purely as robustness context (a wider
2-bar opening range; the New York open — same mechanism, different session), **not** a search for the
best. Model B is the no-edge baseline. Every cell is reported; only the headline is judged.

All numbers are RESULTs on **real Dukascopy** data, train (≤2021-12-31) + validation (2022–2023)
only; the test window is never read. To complete the cross-pair grid, real **M15** bars were ingested
for GBP/USD and USD/JPY (they previously had only H1/H4); Model E is intrinsically intraday, so H4 is
out of scope by construction (a session ≈ 2 H4 bars — no opening range). Leakage/future-invariance is
pinned by 14 Model E unit tests. Reproduce:

```bash
.venv/Scripts/python.exe scripts/measure_session_breakout.py
```

## Evidence

### 1. Headline net expectancy — negative in 11 of 12 cells (per trade, R, normal costs)

| pair | train H1 | train M15 | val H1 | val M15 |
|---|--:|--:|--:|--:|
| EUR/USD | −0.0742 | −0.1687 | −0.1618 | −0.1904 |
| GBP/USD | −0.0628 | −0.1011 | −0.1206 | −0.1869 |
| USD/JPY | −0.0636 | −0.1661 | **+0.0854** | −0.0640 |

Gross expectancy is ≈0 almost everywhere (in-sample gross |t| < 0.8 in 10 of 12 cells): the opening-
range breakout is, before costs, a near coin-flip on direction — there is no raw edge for costs to
erode. The lone exception is the one positive net cell, **USD/JPY val H1** (gross +0.1355, t +2.10,
PF 1.22). +50% cost stress makes every net figure worse. The two robustness-context configs behave
the same (London `or2`: 11/12 net-negative; New York `or1`: 10/12, the two "positives" ≈ 0).

### 2. Walk-forward (purged/embargoed sequential OOS blocks; no parameter is fit) — net-negative everywhere

| pair / TF | net R (all blocks) | t | +50% stress | blocks net>0 |
|---|--:|--:|--:|:--:|
| EUR/USD H1 | −0.0997 | −3.08 | −0.1397 | 1 / 5 |
| EUR/USD M15 | −0.1866 | −5.82 | −0.2789 | 0 / 5 |
| GBP/USD H1 | −0.0848 | −2.64 | −0.1124 | 0 / 5 |
| GBP/USD M15 | −0.1069 | −3.31 | −0.1709 | 1 / 5 |
| USD/JPY H1 | −0.0238 | −0.72 | −0.0619 | 3 / 5 |
| USD/JPY M15 | −0.1428 | −4.44 | −0.2263 | 0 / 5 |

Even the friendliest timeline (USD/JPY H1) is net-negative overall across the full train+val span;
its single positive val block does not make the mechanism robust. Every other timeline is decisively
negative (t −2.6 to −5.8).

### 3. The one positive cell is a USD/JPY-2022/23 regime footprint, not a mechanism edge

Decomposing **USD/JPY val H1** (the only net-positive headline cell):

| USD/JPY val H1 | close drift | headline net | long net | short net | always-long | always-short |
|---|--:|--:|--:|--:|--:|--:|
| London or1 | +22.5% | +0.0854 | **+0.1970** | −0.0464 | +0.0020 | −0.1210 |

The gain is **entirely long-side** (long +0.197 vs short −0.046) during a **+22.5%** USD/JPY move
(the 2022–2023 yen decline). Blind always-long earned only +0.002/trade, so the opening-range
*timing* did concentrate the longs beyond generic "be long in an uptrend" — but the cell is (a) one
pair, (b) one period (**absent in USD/JPY train H1**, −0.0636), (c) one timeframe (**absent on USD/JPY
M15 val**, −0.0640), and (d) net-negative on both other pairs. It is the **same pair+period** where
the unrelated Model D setup also popped positive (ADR 0002). Two mechanistically independent rules
lighting up on exactly USD/JPY-during-2022/23 is the signature of a **pair/period regime** (a
persistent intraday long-trend any "enter with the intraday move" rule catches), not a generalizable
opening-range edge.

## Decision

**Model E fails P4 and is rejected.** P4 needs positive expectancy net of costs on the SAME side,
robust across ≥2 pairs *and* ≥2 periods, stable under +50% stress. Model E clears none of that:
net-negative in 11/12 headline cells, net-negative in all six walk-forward timelines, and its lone
positive cell is a single-pair/single-period long-trend footprint that vanishes in-sample, on the
other timeframe, and on the other pairs.

Consequences, per charter §1.5, §2, §3:

- **Classification (charter §2):** Model E as a standalone edge → **INVALID** (no gross edge; net-
  negative and OOS-negative across pairs/periods; fails stress). The USD/JPY val-H1 long cell →
  **WEAK observation**, explicitly the *same* USD/JPY-2022/23 footprint as the Model D anomaly — an
  observation to note, **never a target to tune toward** (charter §1.3). It is not promoted.
- **No ML.** Baseline-before-ML means there must be a baseline edge to filter; there is none. No ML
  before a P4 GO (charter §3).
- **Test window stays sealed.** Nothing on train/validation clears the bar, so spending the untouched
  2024+ window is unjustified.
- **A failed gate is a legitimate finding.** Five mechanisms — four SMC (A–D) and one non-SMC (E) —
  now fail P4 under identical, honest, leakage-controlled measurement. That is a real result about
  these markets at these timeframes/costs, not a defect to engineer around.

### Observation worth recording (not a P4 candidate)

Two independent mechanisms (Model D FVG-continuation on H4; Model E London opening-range on H1) both
show a net-positive **long/short-timed edge on USD/JPY specifically during the 2022–2023 validation
window**, and nowhere else. This points at a **USD/JPY 2022–23 intraday-trend regime** rather than
any single setup. Per the charter it is an observation to explain, not to tune toward, and it may
**not** be validated on the sealed test window. If ever studied, it should be framed as regime
detection (is there a persistent, detectable intraday-trend state?), tested across pairs and multiple
periods, and replicated on an independent data source before any belief.

## Where this leaves the research

The pre-ML rule-baseline conclusion of ADR 0002 stands and is reinforced: **hold at P4, no ML, test
sealed.** The plan's setup universe (A–D) plus one genuinely new mechanism (E) are all rejected.
Continuing to generate further ad-hoc mechanisms is itself a **multiple-comparisons risk** (enough
tries will eventually surface a lucky in-sample cell), so any further mechanism must be justified by a
prior structural reason to expect an edge — not tried simply because the previous one failed.

No numbers in this record are fabricated; all trace to `scripts/measure_session_breakout.py` and
`experiments/registry.jsonl`.
