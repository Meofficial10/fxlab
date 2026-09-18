# ADR 0008 — Candidate C cross-sectional short-horizon reversal preregistration

- **Status:** Proposed for acceptance before measurement
- **Research gate:** P4 pre-test protocol freeze
- **Candidate:** Candidate C — cross-sectional five-day FX reversal
- **Protocol:** `candidate_c_cross_sectional_reversal.v1`
- **Repository checkpoint audited:** `bf074542d014b58f78feb40e234fb6827742cebb`

No Candidate C return, performance statistic, ranking output, or 2024+ observation was
inspected to select this protocol. This ADR authorizes neither measurement by itself,
opening the sealed test, ML, nor trading.

## 1. Authority and exclusions

The project-wide charter supplies closed-bar causality, chronological splits,
purge/embargo, gross and net reporting, adverse costs, +50% spread/slippage stress,
the P4 robustness dimensions, cryptographic provenance, and the 2024+ seal.

Candidate B's monthly calendar, 2015 start, 83/23/106 counts, Decimal requirement,
fixed one-way costs, HAC lag 3, one-sided 95% Candidate-B gate, three-month bootstrap,
10,000 replications, seed, 50% concentration ceiling, and Candidate-B RunID fields do
not apply. No Candidate B threshold is imported here.

## 2. Frozen hypothesis and primary specification

**Hypothesis.** A currency's extreme relative five-complete-D1-bar move against USD
partly reverses over the next D1 bar because short-lived flow/liquidity imbalance is
not fully permanent information.

There is exactly one primary specification and no parameter grid:

- Universe, in canonical order: `AUDUSD`, `EURUSD`, `GBPUSD`, `NZDUSD`,
  `USDCAD`, `USDCHF`, `USDJPY`.
- Signal bars come only from `dukascopy_direct_d1` /
  `dukascopy_direct_d1_v1` / `dukascopy_direct_bid_d1_v1`.
- For the latest completed bar `t`, the five-bar score is
  `log(close[t] / close[t-5])` for USD-quoted pairs and its negative for
  `USDCAD`, `USDCHF`, and `USDJPY`. It therefore needs six ordered closes.
- Scores are foreign-currency returns versus USD. Lowest two are long foreign
  currency; highest two are short foreign currency.
- A tie crossing either second/third selection boundary causes causal non-entry for
  the whole cohort. Ties are never broken by pair name or an outcome-dependent rule.
- Each selected currency receives signed weight `+0.25` or `-0.25`; gross exposure is
  `1.0`, net foreign-currency weight is `0.0`. No residual weight is redistributed.
- The signal timestamp is the close of bar `t`. Entry uses the first eligible tick at
  or after that timestamp in the established `[00:00, 01:00) UTC` partition. Exit is
  the first eligible tick at the next UTC D1 boundary. Holdings do not overlap;
  exit and the next entry may share a boundary but are separately accounted.
- Structurally valid direct-D1 rows, including zero-volume or flat rows, remain in
  the lookback and may be the signal bar. Volume is not interpreted as a market-open
  label. Execution eligibility comes only from execution evidence.

## 3. Frozen data windows and leakage boundary

Input data are sealed to `[2014-01-01T00:00:00Z, 2024-01-01T00:00:00Z)`.
The first possible signal is `2014-01-07T00:00:00Z`, after six D1 closes establish
the five-bar close-to-close score.

- Train data interval: `[2014-01-01, 2022-01-01)`.
- Validation data interval: `[2022-01-01, 2024-01-01)`.
- Test interval: `[2024-01-01, ...)`, sealed and not read by this protocol.
- A cohort belongs to the split containing its signal. Its signal, entry, and exit
  must all precede that split's exclusive end.
- Purge: a boundary-crossing cohort is excluded. Thus the signal at
  `2021-12-31T00:00:00Z` and the signal at `2023-12-31T00:00:00Z` are excluded.
- Embargo: one complete label horizon follows the train/validation boundary; the
  `2022-01-01T00:00:00Z` signal is excluded. The first validation signal is
  `2022-01-02T00:00:00Z`.
- Historical bars before a split may supply the causal lookback. No fitted parameter,
  scaler, threshold, or rank mapping crosses from validation into train or vice versa.

The verified 2014 and 2015 execution inventories each contain 2,555 scheduled
pair-dates: 1,820 AVAILABLE and 735 EMPTY_EVIDENCED, with no incomplete, conflict, or
corrupt partition. This establishes the 2014 provenance start; it does not turn empty
evidence into a price or make the full-calendar manifest complete.

## 4. Execution-evidence policy

The established Candidate C evidence schema and all-seven-pair cohort assessment are
binding. Only `AVAILABLE` supplies bid, ask, or an execution price.

- `ABSENT_EVIDENCED`, `EMPTY_EVIDENCED`, or `NO_VALID_TICK` at an entry boundary
  causes a recorded causal **non-entry** for the entire seven-pair cohort. It produces
  no trade, turnover, cost, or synthetic price. The calendar equity is explicitly flat
  for that cohort, and state/reason counts are reported.
- `INVALID_PARTITION` or `MISSING_LOCAL_PARTITION` at any required boundary makes the
  split and run `NOT_EVALUABLE`.
- After an entry occurred, any non-AVAILABLE exit for any of the seven pairs makes the
  split and run `NOT_EVALUABLE`. It is not skipped and receives no carry-forward,
  next-hour, Direct-D1-open, or synthetic exit.
- All seven pairs must be AVAILABLE at both boundaries of an entered cohort. There is
  no six-pair fallback, rank-dependent acquisition, or weight redistribution.
- The source manifest remains truthfully incomplete when its `available_count` differs
  from `total_count`; the measurement result may be evaluable only by the explicit
  causal rules above and must report the manifest state counts unchanged.

## 5. Frozen execution and cost accounting

Pair-side mapping is mechanical: long foreign currency buys a USD-quoted pair and
sells a USD-base pair; short foreign currency does the reverse. For USD-base pairs,
executable pair prices are reciprocated only after choosing the adverse pair side.

For each source tick with finite positive `bid <= ask`, let `mid=(bid+ask)/2` and
`half=(ask-bid)/2`. Scenario multiplier `f` is `1.0` headline and `1.5` stress:

- scenario bid is `mid - f*half`; scenario ask is `mid + f*half`;
- per-side slippage is `f * 0.2 * pip_size`, with pip size `0.01` for USDJPY and
  `0.0001` otherwise;
- buy fill is scenario ask plus slippage; sell fill is scenario bid minus slippage;
- observed spread is never charged a second time and the configured 0.6-pip fallback
  is not used when bid/ask evidence exists;
- `slippage_vol_coeff` is evaluated at the existing execution convention
  `norm_vol=0.0`; no new volatility estimator is introduced;
- commission remains USD 7.00 per standard-lot round trip and is not stress-scaled.

Accounting starts each split at unit USD equity. Each selected leg receives absolute
USD notional `0.25 * pre-entry equity`. Lots are derived from a 100,000-base-unit
standard lot: USD-quoted base units equal allocated USD divided by entry USD-per-
foreign fill; USD-base units equal allocated USD. Instrument PnL and quote-currency
conversion use the adverse executable exit prices. Commission scales linearly by lots
and is deducted in USD. Gross excludes commission and modeled slippage but uses the
same observed midpoints; headline and stress use the adverse fills above. A changed
cost value, fill formula, pair orientation, or valuation rule changes policy identity.

The cost fingerprint binds the canonical cost inputs, exact `config/costs.yaml`
content hash, pair pip sizes, observed-spread rule, slippage input/rule, commission,
stress multiplier, orientation/valuation rule, and execution-evidence identity.

## 6. Metrics and exact units

Every metric is reported separately for train and validation, headline and stress.
Returns are unit-equity USD fractions, not percentages unless display-only conversion
is labelled.

- `trade_count`: completed round-trip legs; an entered cohort contributes exactly 4.
- `cohort_count`: completed four-leg cohorts; non-entry counts are separate by reason.
- `net_expectancy`: arithmetic mean completed-cohort net portfolio return.
- `gross_expectancy`: corresponding gross mean.
- `annualized_return`: geometric growth of the calendar equity curve raised to
  `365.2425 / calendar_days_in_split`, minus one.
- `Sharpe`: arithmetic mean divided by sample standard deviation (`ddof=1`) of the
  complete UTC calendar-day net return series, times `sqrt(365.2425)`, risk-free zero.
  A day with causal non-entry is explicitly flat; it is not an inferred execution.
- `MaxDD`: maximum magnitude of `equity/running_peak - 1`, including initial equity 1.
- `cost_drag`: both gross minus net expectancy and gross minus net annualized return.

All sums use deterministic canonical order and stable summation. NaN, infinity,
nonpositive equity, zero completed cohorts, or undefined sample variance makes the run
`NOT_EVALUABLE`, not zero.

## 7. Statistical and multiplicity policy

Candidate C is a single primary test. No exploratory variant may replace it. The
repository records seven prior performance-tested mechanism families; Candidate C is
treated conservatively as test 8. Bonferroni family-wise one-sided alpha is therefore
`0.05 / 8 = 0.00625`. Candidate B was infeasible before performance and consumes no
test in this count. No claim is made that earlier work used a formal alpha budget.

Validation uncertainty uses an intercept-only Newey-West estimate on the complete
calendar-day net return series, Bartlett weights, gamma denominator `n`, no small-
sample multiplier, and automatic lag
`floor(4 * (n / 100) ** (2 / 9))`. The lower confidence bound is
`mean - scipy.stats.t.ppf(0.99375, n-1) * standard_error`. It is computed separately
for headline and stress. This automatic daily rule is newly adopted for Candidate C;
it is not Candidate B's fixed lag 3. No random bootstrap or seed is used.

Robustness reporting also includes 2022 and 2023 separately and each pair's cumulative
net contribution. No numerical concentration ceiling is imposed; equal ex-ante weights
already bind exposure, while ex-post contribution concentration is reported only.

## 8. Deterministic P4 decision contract

`GO` here means only **GO_TO_SEPARATELY_AUTHORIZED_SEALED_TEST**. It is not VALIDATED,
does not open 2024+, does not authorize ML, and does not authorize trading.

The result is `NOT_EVALUABLE` if any required identity/provenance check fails, any
invalid/missing partition exists, an entered cohort lacks a complete exit, accounting
or inference is undefined, no cohort completes, leakage is detected, or 2024+ is read.

Otherwise `GO` requires every condition below; failure of any one is `NO_GO`:

1. Train headline and stress net expectancy are strictly positive.
2. Validation headline and stress net expectancy and annualized return are strictly
   positive, and headline and stress Sharpe are strictly positive.
3. The validation Newey-West lower bound is strictly positive for headline and stress.
4. Validation net expectancy is strictly positive in each of 2022 and 2023 under both
   headline and stress costs.
5. At least two pairs have strictly positive cumulative validation net contribution
   under both headline and stress.
6. Cost drag is finite and nonnegative, MaxDD is finite and strictly less than 1, all
   chronology/future-invariance checks pass, and no decision input was changed after
   measurement began.

There is no tunable minimum Sharpe, annual return, expectancy, drawdown, hit rate, or
trade-count threshold. Strict positivity and the globally required robustness axes are
the only economic cutoffs.

## 9. Run identity and reproducibility

Before measurement, implementation must build an immutable semantic policy object from
this ADR. Its `policy_id` is repository `canonical_sha256` over that object and binds the
SHA-256 of the exact ADR bytes. Measurement is forbidden from a dirty worktree.

The run input identity must bind at least:

- candidate/protocol names and versions, ADR byte hash, and policy ID;
- clean Git commit and measurement implementation/schema version;
- canonical ordered universe and direct-D1 provider, normalization, source revisions,
  content hashes, query fingerprints, dataset IDs, and sealed bounds for all pairs;
- execution-evidence schema, ordered record identities, manifest ID/state counts, and
  entry/exit policy (including non-entry and NOT_EVALUABLE rules);
- train/validation/test boundaries, earliest signal, purge and embargo;
- score/orientation, rank/tie rule, selected counts, weights, zero-volume policy,
  timestamps, and holding rule;
- cost fingerprint and headline/stress rules;
- metric, uncertainty, multiplicity, and GO/NO_GO/NOT_EVALUABLE policy versions.

The `run_id` is `canonical_sha256` of those semantic inputs only. Results, retrieval
timestamps, local paths, filesystem mtimes, retry/completion order, and display format
do not enter it. Audit identities remain separately retained. Deterministic pair/date
ordering is mandatory. This protocol uses no randomness; adding randomness requires a
new protocol version and a bound generator/seed.

Same semantic inputs + same clean code + same policy must produce byte-identical
canonical results and the same run ID. Any decision-relevant change requires a new ADR
and protocol version before another measurement; it may not overwrite v1.

## 10. Authorization boundary

After review and acceptance, implementation of a measurement module may begin without
reading outcomes during construction. Running it remains a separate explicit action.
The 2024+ test, ML, execution, and real-money trading remain unauthorized.
