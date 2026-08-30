# ADR 0006 — R2 Candidate B public policy-rate differential preregistration

- **Status:** Accepted (2026-08-29) — preregistration only
- **Research gate:** R2 hypothesis and measurement-design preregistration
- **Candidate:** Candidate B — public policy-rate differential → subsequent one-month spot-FX
  return
- **Decision:** **CANDIDATE B R2 PREREGISTERED.** Data ingestion, R4, R6, R7/R8, strategy
  execution, live trading, and access to the sealed 2024+ window are **not authorized**.
- **Audited repository checkpoint:** `ca33f21c6834d9e124ecdd7a738196c7de451d9e`

> **At the time of this preregistration, no Candidate B return series,
> performance statistic, backtest result, validation result, or 2024+
> observation has been inspected or calculated.**

## Context and boundary

Candidate B is a separate public-data research hypothesis. It asks whether publicly observable,
currently effective central-bank policy-rate differentials predict subsequent normalized spot-FX
returns. It is a hypothesis about public monetary-policy state; it is **not** executable OTC carry,
forward-discount carry, policy-rate accrual, Candidate A, or a trading authorization.

Candidate A remains unchanged:

- **Candidate A:** exact OTC 1M forward carry
- **Status:** **PARKED / R2 BLOCKED — DATA CONTRACT NOT PROVEN**

Candidate B cannot validate Candidate A. No Candidate B result can be interpreted as evidence that
spot policy-rate differences equal an investable forward discount or that the returns below include
financing income.

## Frozen economic hypothesis

At monthly formation, currencies with higher currently effective central-bank policy-rate
differentials relative to USD are hypothesized to have higher subsequent one-month normalized
spot-FX returns than lower-rate currencies.

The proposed mechanism is compensation for exposure to monetary-policy, funding-liquidity, crash,
and risk-off unwinding risk. Higher-rate currencies may attract capital but can suffer abrupt
reversals when global risk tolerance falls or domestic policy expectations change. That risk can
support a persistent premium without implying an arbitrage.

Expected failure regimes include rapid policy reversals, global flight-to-quality episodes,
convergence near zero rates, differentials already fully priced in forwards, capital controls, and
spot moves dominated by non-rate shocks. No interest differential is accrued into PnL. The measured
outcome is subsequent spot return only.

## Frozen universe

The universe is exactly seven provenance-established FXLab pairs:

| Pair | Foreign currency | Quotation |
|---|---|---|
| `AUDUSD` | `AUD` | direct |
| `EURUSD` | `EUR` | direct |
| `GBPUSD` | `GBP` | direct |
| `NZDUSD` | `NZD` | direct |
| `USDCAD` | `CAD` | inverse |
| `USDCHF` | `CHF` | inverse |
| `USDJPY` | `JPY` | inverse |

The reference currency is `USD`. No pair or currency may be added, removed, or substituted after
measurement begins. Every formation requires all seven foreign-currency signals and all seven spot
prices; there is no dynamic-universe fallback.

## Frozen BIS signal family

Use only the BIS-defined principal policy-rate history identified by:

- Agency: `BIS`
- Dataflow: `WS_CBPOL`
- Version: `1.0`
- Frequency: `D`

| Currency | BIS series |
|---|---|
| `AUD` | `D.AU` |
| `CAD` | `D.CA` |
| `CHF` | `D.CH` |
| `EUR` | `D.XM` |
| `GBP` | `D.GB` |
| `JPY` | `D.JP` |
| `NZD` | `D.NZ` |
| `USD` | `D.US` |

The BIS-defined history and documented instrument splices are binding. Researcher-selected
alternative policy instruments, alternative short-rate families, or hand-built substitutions are
forbidden. Before R4, the exact BIS data-structure definition, codelists, units, observation status,
series metadata, and instrument segments must be frozen in immutable provenance artifacts.

## Frozen point-in-time contract

For formation month `m`:

- `F_m` is the close timestamp of the last common fully closed FXLab D1 observation in month `m`.
- `C_m` is `00:00:00 UTC` at the beginning of `F_m`'s UTC calendar date.
- A policy-rate state is eligible only when its authoritative announcement timestamp is at or
  before `C_m` **and** its effective date/time is at or before `C_m`.
- Same-day decisions are excluded, even if announced before the D1 close.
- Future-effective decisions are excluded until effective.
- Carrying forward a rate already known and effective is allowed as state persistence.
- Backward filling from future-known values is forbidden.
- Ambiguous, missing, or conflicting announcement/effective-time evidence invalidates the complete
  cohort.

The current BIS historical series is not, by itself, proof of what was known at each formation.
Before R4, every relevant rate change must be reconciled to an immutable inventory of authoritative
central-bank announcement and effective-date evidence. An unresolved BIS-to-official discrepancy
invalidates the affected cohort.

## Frozen formation and holding rule

- Form once per calendar month at `F_m`.
- Enter using the `F_m` D1 close.
- Exit using the `F_{m+1}` D1 close.
- Hold for exactly one formation-to-formation interval.
- Do not rebalance between formation dates.
- Do not exit early.
- Purge any cohort crossing the train/validation boundary.
- If a valid common formation does not exist, keep it missing; do not shift formation based on
  outcomes or to improve coverage.
- Label a cohort by its exit date/month.

Train and validation are independent portfolios. Train is liquidated at its terminal boundary;
validation begins from zero.

## Frozen signal

For foreign currency `c` at formation `m`:

```text
d_c,m = i_c(C_m) - i_USD(C_m)
```

Rates and differences use percentage points. The signal is used only for ordinal ranking. It must
not be compounded, tenor-converted, transformed into expectations, used to reconstruct a forward,
accrued as interest, or normalized another way.

## Frozen ranking and portfolio

Rank all seven foreign currencies by descending `d_c,m`:

- top two: long, weight `+0.25` each;
- bottom two: short, weight `-0.25` each;
- middle three: weight `0`;
- gross exposure: `1.00`;
- net currency weight: `0.00`;
- exact ties: ascending ISO currency-code lexical order.

All seven signals are required. Volatility scaling, leverage, regime conditioning, dynamic
universes, best-N searches, side selection, and return-dependent weights are forbidden.

## Frozen return normalization and estimand

Every currency return is normalized so positive means the foreign currency appreciates against
USD. Let `V_c(t)` be USD value per unit of foreign currency at time `t`:

- for `AUDUSD`, `EURUSD`, `GBPUSD`, and `NZDUSD`, `V_c(t)` is the direct pair value;
- for `USDCAD`, `USDCHF`, and `USDJPY`, `V_c(t) = 1 / pair_price(t)`.

The primary currency return is:

```text
r_c,m+1 = V_c(F_m+1) / V_c(F_m) - 1
```

The monthly gross portfolio return is the sum of each frozen formation weight multiplied by its
normalized currency return. The **primary estimand** is the arithmetic mean monthly **net** spot-FX
portfolio return. Supporting statistics, including cumulative return, Sharpe ratio, drawdown, and
confidence intervals, may not replace the primary estimand.

## Frozen transaction-cost contract

Use these one-way return-space research assumptions:

| Pair | One-way cost |
|---|---:|
| `EURUSD` | 1.0 bp |
| `GBPUSD` | 1.0 bp |
| `USDJPY` | 1.0 bp |
| `AUDUSD` | 1.2 bp |
| `USDCAD` | 1.2 bp |
| `USDCHF` | 1.2 bp |
| `NZDUSD` | 1.5 bp |

At a weight change, cost is:

```text
sum(one_way_cost_in_return_units * abs(new_weight - old_weight))
```

where one basis point is `0.0001` return units. Initial entry from zero is charged. Terminal
liquidation of each split to zero is charged to that split. Validation starts from zero.

Terminal liquidation does not create another monthly cohort or return observation. Its turnover
cost is charged to the final measured cohort of that split. Train and validation therefore remain
exactly `83` and `23` measured cohorts, respectively, and validation still starts independently
from zero.

- Headline cost multiplier: `1.0`.
- Mandatory stress multiplier: `1.5`.

No financing accrual, forward spread, cross-currency basis, broker swap, or fabricated carry income
is permitted. These costs are frozen research assumptions, not historical executability proof.

## Frozen accounting and summary-statistic conventions

Each split starts with normalized equity `equity_0 = 1.0`. For its ordered net monthly cohort
returns `r_1, ..., r_n`, equity compounds as:

```text
equity_t = equity_(t-1) * (1 + r_t)
```

Drawdown at `t` is:

```text
drawdown_t = equity_t / max(equity_0, ..., equity_t) - 1
```

Maximum drawdown is the magnitude of the minimum `drawdown_t`. Initial equity `1.0` participates as
the initial peak, and train and validation drawdowns are calculated independently.

Annualized Sharpe uses only net monthly cohort returns, an arithmetic mean, risk-free rate `0`,
sample standard deviation with `ddof=1`, and annualization by `sqrt(12)`:

```text
Sharpe = sqrt(12) * mean(r) / sample_std(r, ddof=1)
```

Nonfinite input, fewer than two observations, or zero sample standard deviation cannot produce a
passing Sharpe result. No alternative annualization or downside-ratio substitution is permitted.

## Frozen research windows and sealed boundary

- **TRAIN:** outcomes ending on or before `2021-12-31`.
- **VALIDATION:** outcomes from `2022-01-01` through `2023-12-31`.
- **TEST:** `2024-01-01+` — **SEALED**.

The train/validation boundary-crossing cohort is purged. No 2024+ observation may be requested or
inspected for metadata, feasibility, imputation, calibration, coverage, analysis, or validation.

## Frozen multiple-comparison budget

There is exactly one decision-making specification:

- one hypothesis;
- one seven-pair universe;
- one BIS signal family;
- one USD-relative signal;
- one monthly schedule;
- one one-month holding period;
- one top-two/bottom-two portfolio;
- one equal-weight rule;
- one headline cost contract;
- one `+50%` cost stress;
- one primary estimand;
- one dependence-aware inference specification.

After outcomes exist, there may be no change to the rate family, lag, tail size, weighting, side,
schedule, start date, regime, subset, or cost specification. Robustness cells are rejection
diagnostics only; none may define a replacement specification or rescue a rejected headline.

## Preregistered cohort expectation

The BIS acquisition start of `2014-01-01` supplies initialization history only, so the
already-announced and already-effective policy state can be established before the first
formation. Calendar year 2014 is not part of the Candidate B portfolio-formation window. The
first formation is January 2015, with its first measured outcome exiting in February 2015.

Train formation months are January 2015 through November 2021 (`83`). The December 2021
formation, which exits in January 2022, is **PURGED** because it crosses the train/validation
boundary. Validation formation months are January 2022 through November 2023 (`23`). A December
2023 formation is excluded because its next formation exit would require sealed 2024 data. The
required measured total therefore remains exactly `106` complete cohorts.

Missing required observations or evidence do not permit shifting the January 2015 formation
start, shrinking the seven-currency universe, imputing observations, or replacing the exact
`83`/`23`/`106` requirement.

Calendar arithmetic produces these expectations:

- train: `83` complete monthly cohorts;
- validation: `23` complete monthly cohorts;
- total: `106` complete monthly cohorts.

These are calendar-derived preregistration expectations, **not evidence that the underlying
market/rate observations exist**. Future data acceptance must independently prove exactly 106
complete cohorts. If fewer than 106 valid cohorts exist, Candidate B is **REJECTED AS INFEASIBLE**.
The count may not be reduced after data inspection.

## Frozen statistical and prospective-power method

The statistical unit is one monthly portfolio cohort.

Primary inference:

- one-sided HAC/Newey–West mean-return inference;
- fixed lag: `3`;
- Student-t reference with `n - 1` degrees of freedom.

For the intercept-only mean inference, let `e_t = r_t - mean(r)`. With `n` net monthly returns and
fixed `L = 3`, define:

```text
gamma_j = (1 / n) * sum from t=j to n-1 of (e_t * e_(t-j))
w_j = 1 - j / (L + 1)
LRV = gamma_0 + 2 * sum from j=1 to L of (w_j * gamma_j)
Var(mean) = LRV / n
SE(mean) = sqrt(Var(mean))
```

No additional finite-sample covariance multiplier is applied. The one-sided 95% lower confidence
bound is:

```text
LCB_95 = mean(r) - t_(0.95, n-1) * SE(mean)
```

HAC passes only when `LCB_95 > 0`. The later implementation must use one reviewed deterministic
Student-t quantile implementation already available in the locked dependency set if possible. If
none is suitable, G2 implementation must stop and report that blocker rather than silently invent
a quantile implementation.

Confirmation:

- circular moving-block bootstrap;
- fixed block length: `3`;
- replications: `10,000`;
- seed: `20260829`.

The bootstrap statistic is the arithmetic mean monthly net return. It uses NumPy `Generator` with
`PCG64`. Each replication draws `ceil(n / 3)` independent block-start indices uniformly from the
integer range `[0, n)`. A block beginning at `start` contains `start`, `start+1`, and `start+2`,
each modulo `n`. Blocks are concatenated in draw order and truncated to exactly `n` observations;
the mean of that resampled vector is recorded.

The bootstrap one-sided 95% lower bound is the 5th percentile of the `10,000` bootstrap means using
NumPy quantile method `linear`. Bootstrap passes only when this lower bound is strictly greater
than zero. No alternate seed, block length, replication count, interval type, or quantile method is
permitted. The NumPy version and RNG implementation identity must be included in G1's late-bound
environment identity.

Validation requires a strictly positive one-sided 95% lower confidence bound under both methods.
No lag, block length, seed, inference method, or confidence level may be selected after outcomes are
known.

With 23 validation observations, the preregistered iid approximation for 80% power at one-sided 5%
significance requires a standardized monthly mean of approximately:

```text
(z_0.95 + z_0.80) / sqrt(23) = 0.519 monthly standard deviations
```

This is roughly an annualized Sharpe of `1.80` before dependence adjustment. This calculation uses
no Candidate B returns. It records that validation is a low-powered, strict screening experiment;
low power cannot justify relaxed gates or post-hoc rescue.

## Frozen economic and drawdown thresholds

Validation must satisfy both:

- mean net monthly return `>= 10 bp`; and
- annualized net Sharpe `>= 0.50`.

These thresholds were fixed before any Candidate B outcome was measured. Ten basis points requires
a result materially larger than the existing assumed implementation drag, while the Sharpe floor
prevents a trivially small or unstable mean from qualifying.

Maximum peak-to-trough drawdown must be **strictly less than 20%** separately in train and
validation. A drawdown `>= 20%` rejects Candidate B. The limit inherits FXLab's existing risk
boundary and was not calibrated to Candidate B outcomes.

## Frozen robustness gates

All of the following are mandatory:

- positive train net mean;
- positive validation net mean;
- positive net mean in calendar 2022;
- positive net mean in calendar 2023;
- at least 4 of 6 predefined chronological blocks positive;
- at least 6 of 7 leave-one-currency-out portfolios positive in train **and** at least 6 of 7
  positive in validation;
- no currency contributes more than 50% of total absolute cumulative contribution within either
  split;
- headline costs pass;
- `+50%` cost stress passes;
- no quotation-direction dependence;
- no leakage or future-data dependence.

For the frozen currency-concentration gate, define each currency's gross monthly contribution as:

```text
gross_contribution_c,m = weight_c,m * normalized_return_c,m
currency_cost_c,m = one_way_cost_c * abs(weight_c,m - weight_c,m-1)
net_contribution_c,m = gross_contribution_c,m - currency_cost_c,m
```

The initial prior weight is zero. The currency's terminal-liquidation cost is allocated to the
final measured cohort of the split. The corresponding headline multiplier `1.0` or stress
multiplier `1.5` is applied to `currency_cost_c,m` for the portfolio being evaluated. Within each
split:

```text
C_c = sum over m of net_contribution_c,m
share_c = abs(C_c) / sum over j of abs(C_j)
```

The concentration gate fails when any `share_c > 0.50`. If the denominator is zero, concentration
is undefined and cannot pass. Profitable-only contributions and sums of absolute monthly returns
must not replace this definition.

The frozen no-quotation-direction-dependence requirement is an implementation and data-
normalization invariant. Economically identical direct foreign/USD values `V` and inverse
USD/foreign prices `1/V` must produce the same normalized foreign-currency return after the ADR's
reciprocal rule. This is verified mechanically with synthetic golden cases; it does not authorize
alternate real datasets, pair orientations, or a second decision-making specification.

The 106 ordered complete cohorts are partitioned deterministically into six consecutive blocks: the
first four blocks contain 18 cohorts each and the final two contain 17 cohorts each. The partition
is based only on chronology. Leave-one-currency-out diagnostics remove each of the seven currencies
once and rerank the remaining six using the same top-two/bottom-two rule. These diagnostics cannot
define a new universe or alternative headline.

## Frozen cost-stress gate

Multiply every one-way cost by exactly `1.5`. Stressed validation must retain:

- positive mean;
- mean `>= 10 bp/month`;
- annualized Sharpe `>= 0.50`;
- positive 2022;
- positive 2023;
- drawdown `< 20%`;
- positive one-sided HAC 95% lower bound;
- positive one-sided bootstrap 95% lower bound.

Any failure rejects Candidate B.

## Immediate rejection conditions

Candidate B is rejected immediately for any of the following:

- missing, ambiguous, or invalid announcement/effective-date evidence;
- unresolved BIS-to-official-source discrepancy;
- incomplete or incompatible BIS metadata;
- fewer than 106 complete cohorts;
- any request for or inspection of 2024+ observations;
- future, backward-filled, same-day, ambiguous, or otherwise invalid point-in-time handling;
- dynamic universe, missing required spot data, or imputation;
- quotation/sign normalization error;
- nonpositive train or validation net mean;
- failure of either economic threshold;
- failure of either inference method;
- nonpositive 2022 or 2023;
- fewer than 4 of 6 positive chronological blocks;
- fewer than 6 of 7 positive leave-one-currency-out portfolios in either split;
- more than 50% absolute cumulative contribution from one currency in either split;
- headline or cost-stress failure;
- drawdown `>= 20%` in either split;
- post-hoc specification modification;
- any multiple-comparison-budget violation.

A negative result is a successful research outcome. No rejection may be rescued by another cell,
subset, interpretation, or altered threshold.

## Candidate A diligence boundary

Candidate A remains **PARKED / R2 BLOCKED**. Candidate B cannot validate Candidate A and cannot
authorize a dataset purchase.

Further Candidate A data diligence is permitted only if every Candidate B R4/R5 gate passes. Even
then, Candidate A's exact OTC 1M-forward contract must independently establish bid/ask quotes,
tenor, synchronized spot, observation timestamps, value dates, conventions, historical coverage,
provenance, licensing, and reproducibility. A separate budget ceiling approved before Candidate B
results must exist before any purchase. No purchase occurs automatically.

## Data and provenance gate before R4

Measurement remains **BLOCKED** until all of the following are independently established:

- BIS DSD, codelists, version, units, status fields, and series metadata are frozen;
- all eight BIS series resolve exactly;
- an authoritative central-bank announcement/effective-date inventory is created;
- BIS-to-official-event concordance passes;
- exactly 106 complete cohorts are established without imputation;
- request bounds mechanically prevent access after `2023-12-31`;
- FXLab spot dataset identities and content fingerprints are frozen;
- provider, mapping, normalization, and D1-construction identities are frozen;
- direct/inverse quotation-normalization tests pass;
- the cost and statistical contracts in this ADR are frozen in the measurement identity;
- immutable dataset, cohort, source, and retrieval manifests are frozen.

Required future provenance includes the audited repository commit, ADR hash/version, BIS dataflow
and series identifiers, source and authoritative event references, retrieval timestamp, byte count,
media type, raw-content SHA-256, request interval, observation metadata, concordance result, spot
dataset fingerprints, closed-bar timestamps, formation/purge manifest, cost fingerprint, and
deterministic code/environment/run identity.

R2 approval does **not** authorize R4.

## Frozen future file boundary

If separately authorized, future work may propose creating only the directly relevant artifacts:

- `src/fxlab/data/policy_rates.py`
- `scripts/ingest_bis_policy_rates.py`
- `scripts/measure_policy_rate_differential.py`
- `tests/test_policy_rate_data.py`
- `tests/test_policy_rate_differential.py`
- `data/manifests/bis_cbpol_candidate_b.json`
- `data/manifests/candidate_b_spot_panel.json`

`experiments/registry.jsonl` may be appended only when separately authorized. This ADR does not
create or authorize any of those files. It does not authorize changes to strategy, provider,
execution, broker, risk, recovery, reconciliation, or Candidate A code.

## Decision record

### Decision

**CANDIDATE B R2 PREREGISTERED.** The hypothesis, universe, data family, point-in-time contract,
formation, signal, portfolio, estimand, costs, windows, statistical method, thresholds, robustness,
stress, and rejection rules are frozen before measurement.

### Alternatives considered and rejected

- Executable OTC 1M forwards: remains Candidate A and lacks a proven data contract.
- Forward reconstruction from policy rates: not economically equivalent and forbidden.
- Alternative short-rate families or researcher-selected instruments: add uncontrolled choices.
- Dynamic or optimized universes, tail sizes, weights, lags, or regimes: violate the frozen
  multiple-comparison budget.
- Opening 2024+ for feasibility or metadata: violates the sealed-window boundary.

### Remaining blockers

Data ingestion remains blocked pending BIS metadata freezing, authoritative announcement/effective-
date inventory construction, BIS concordance, licensing/provenance confirmation, pre-2024 request
enforcement, and deterministic proof of exactly 106 complete cohorts. R4 remains separately blocked
until every data/provenance gate above passes. The short validation window remains a known power
limitation and cannot be addressed by weakening this preregistration.

## Authorization statement

- **CANDIDATE B R2 PREREGISTERED**
- **DATA INGESTION NOT AUTHORIZED**
- **R4 NOT AUTHORIZED**
- **R6 NOT AUTHORIZED**
- **R7/R8 NOT AUTHORIZED**
- **LIVE TRADING NOT AUTHORIZED**
- **2024+ REMAINS SEALED**
