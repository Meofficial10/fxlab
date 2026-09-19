# ADR 0011 — Candidate D time-series momentum preregistration

- **Status:** Proposed for acceptance before Candidate D measurement
- **Research gate:** P4 prospective protocol preregistration
- **Candidate:** Candidate D — G10 time-series trend momentum
- **Protocol:** `candidate_d_time_series_momentum.v1`
- **Supersedes:** Nothing; Candidate C is CLOSED under ADR 0010
- **Repository checkpoint audited:** `46f3357107fc3f3ad99e34fdaaf9596081935f11`

No Candidate D return, performance statistic, backtest result, hypothetical ranking, or 2024+ observation was inspected to select this protocol. This ADR authorizes neither measurement by itself, opening the sealed test, ML, demo/live execution, nor real-money trading.

---

## 1. Master Principles and Research Governance

This preregistration strictly adheres to FXLab core research tenets:

- **INFRASTRUCTURE CONFIDENCE != TRADING EDGE.** Data infrastructure validation does not imply alpha.
- **AI RESEARCH != PERMISSION TO TRADE.** Candidate D is a research accounting hypothesis only.
- **NO EDGE and NOT_EVALUABLE are acceptable research outcomes.**

Candidate D is entirely independent from Candidate C. It evaluates individual currency pair time-series momentum rather than cross-sectional ranking reversal.

---

## 2. Hypothesis and Economic Rationale

### Hypothesis
Intermediate-term (20-D1-interval) directional price trends in individual major FX pairs persist sufficiently over the subsequent 20-tradable-session holding period to generate positive net economic performance after accounting for observed bid/ask spreads, adverse execution fills, slippage, and broker commissions.

This is an empirical hypothesis, not an edge claim.

### Economic Rationale
Capital flow inertia, macroeconomic information diffusion, and monetary policy rate cycles create persistent medium-term directional trends in major foreign exchange rates.

---

## 3. Universe and Orientation

### Universe
The formation and trading universe consists of the seven canonical FX pairs in fixed canonical order:

1. `AUDUSD`
2. `EURUSD`
3. `GBPUSD`
4. `NZDUSD`
5. `USDCAD`
6. `USDCHF`
7. `USDJPY`

Each currency pair is evaluated and traded **completely independently**. There is no cross-sectional ranking, no top/bottom quantile sorting, and no requirement for simultaneous availability across all seven pairs.

### Native Quoting Orientation
Signals and trades are constructed in the native quoted orientation of each pair:
- `AUDUSD`, `EURUSD`, `GBPUSD`, `NZDUSD`: Positive return -> Long pair (Long foreign currency, Short USD).
- `USDCAD`, `USDCHF`, `USDJPY`: Positive return -> Long pair (Long USD, Short foreign currency).

Do not apply Candidate C's foreign-currency USD-base sign negation.

---

## 4. Signal, Bar Availability, and Causal Timing

### Signal Horizon and Observation Count
For each pair $i$ at completed D1 bar $t$, the signal is computed from the log return over a 20-D1-bar interval, requiring exactly 21 ordered, valid completed D1 close observations ($t-20, t-19, \dots, t$):

$$\text{raw\_return}(i, t) = \ln\left(\frac{\text{close}(i, t)}{\text{close}(i, t-20)}\right)$$

- $s(i, t) = +1$ if $\text{raw\_return}(i, t) > 0$
- $s(i, t) = -1$ if $\text{raw\_return}(i, t) < 0$
- $s(i, t) = 0$ if $\text{raw\_return}(i, t) == 0$

### Valid Signal Row Definition
Signal bars come exclusively from the verified `dukascopy_direct_d1` / `dukascopy_direct_d1_v1` / `dukascopy_direct_bid_d1_v1` provider. Under the Direct-D1 contract:
- A structurally valid D1 row satisfies finite positive OHLC prices ($\min(\text{open}, \text{high}, \text{low}, \text{close}) > 0$, $\text{high} \ge \max(\text{open}, \text{low}, \text{close})$, $\text{low} \le \min(\text{open}, \text{high}, \text{close})$), finite non-negative volume ($\text{volume} \ge 0$), and an exact UTC midnight open timestamp ($\text{ts\_open} \pmod{86400} == 0$).
- Structurally valid direct-D1 rows, including zero-volume or flat calendar-day closure rows, remain in the lookback and qualify as valid signal closes. Volume is not interpreted as a market-open filter.
- Execution eligibility is governed exclusively by execution evidence at the 00h boundary, never by bar volume.

### Causal Bar-to-Entry Mapping
Under the Direct-D1 contract, a D1 bar with open timestamp $\text{ts\_open}(t)$ covers the 24-hour interval $[\text{ts\_open}(t), \text{ts\_open}(t) + 24\text{h})\text{ UTC}$.
- **Final Signal Bar:** D1 bar $t$ identified by $\text{ts\_open}(t)$.
- **Bar Completion Timestamp:** The bar completes exactly at $\text{SIGNAL\_KNOWN\_AT} = \text{ts\_open}(t) + 1\text{ calendar day}$ ($00:00:00\text{Z}$ UTC).
- **Earliest Execution Boundary:** $\text{ENTRY\_BOUNDARY} = \text{ts\_open}(t) + 1\text{ calendar day}$ ($00:00:00\text{Z}$ UTC).
- **Execution Partition:** The hourly tick partition $[\text{ENTRY\_BOUNDARY}, \text{ENTRY\_BOUNDARY} + 1\text{h})\text{ UTC}$.

All 21 required close observations ($t-20 \dots t$) are strictly in the past before the execution partition opens.

### Earliest Eligible Entry
With research input starting `2014-01-01T00:00:00Z` (index 0, $\text{ts\_open} = \text{2014-01-01}$), the first 21 completed close observations are indices 0 through 20 ($\text{ts\_open} = \text{2014-01-01}$ through $\text{ts\_open} = \text{2014-01-21}$).
Bar index 20 completes at `2014-01-22T00:00:00Z`.
**Earliest eligible entry boundary:** `2014-01-22T00:00:00Z`.

---

## 5. Entry, Holding, and Exit Rules

### Entry Rule
A pair $i$ enters a new position at boundary $T$ if and only if:
1. Signal $s(i, t) \ne 0$;
2. Pair $i$ has no currently active position;
3. Pair $i$'s execution evidence at boundary $T$ is `AVAILABLE`.

*Execution Fill:* First eligible tick in the $[00:00, 01:00)\text{ UTC}$ execution partition:
- Long ($s=+1$): Enters at `Ask` price plus modeled slippage.
- Short ($s=-1$): Enters at `Bid` price minus modeled slippage.

*Non-Entry & Fail-Closed:*
- If execution state is `EMPTY_EVIDENCED` or `ABSENT_EVIDENCED`: Causal `NON_ENTRY` for pair $i$ (flat return). No next-hour or cross-pair substitution.
- If execution state is `NO_VALID_TICK`, `INVALID_PARTITION`, or `MISSING_LOCAL_PARTITION`: Measurement terminates immediately as `NOT_EVALUABLE`.

### Holding Rule (20 Tradable Sessions)
Holding duration is defined strictly using execution evidence:
- At entry boundary $T_{\text{entry}}$, the session counter for pair $i$ is initialized to $c = 0$ (entry day is session zero).
- At each subsequent daily 00h boundary $T = T_{\text{entry}} + k\text{ days}$ ($k \ge 1$):
  - If pair $i$'s execution evidence is `AVAILABLE`: $c \leftarrow c + 1$.
  - If pair $i$'s execution evidence is `EMPTY_EVIDENCED` or `ABSENT_EVIDENCED`: $c$ is unchanged (holding through market closure).
  - If pair $i$'s execution evidence is `NO_VALID_TICK`, `INVALID_PARTITION`, or `MISSING_LOCAL_PARTITION`: Fail closed as `NOT_EVALUABLE`.
  - When $c = 20$: Boundary $T$ is the **exit boundary**.

If $c < 20$ after 40 calendar days from entry, the measurement fails closed as `NOT_EVALUABLE` with reason `exit_horizon_exceeded`.

### Exit Rule
At the resolved exit boundary $T_{\text{exit}}$ ($c = 20$), the position exits using the first eligible tick in the $[00:00, 01:00)\text{ UTC}$ partition:
- Long exit sells at `Bid` minus modeled slippage.
- Short exit buys at `Ask` plus modeled slippage.

### Same-Boundary Re-entry Rule
When pair $i$ exits at boundary $T_{\text{exit}}$, that same boundary is eligible to evaluate a new signal and enter a new position.
The causal sequence at boundary $T$ is:
1. **Exits execute first** (settling position and realizing cash P&L);
2. **Signal evaluated second** (on the newly completed D1 bar);
3. **Entries execute third** (using post-exit portfolio equity snapshot).

Exit and entry legs are accounted as separate transactions with independent spreads, slippage, and commissions. No synthetic netting or spread bypass is permitted.

---

## 6. Position Sizing and Portfolio Accounting

### Sizing Baseline
At each boundary $T$:
1. Settle all exits in canonical pair order (`AUDUSD`, `EURUSD`, `GBPUSD`, `NZDUSD`, `USDCAD`, `USDCHF`, `USDJPY`), realizing exit P&L and updating cash.
2. Establish a single post-exit/pre-entry portfolio equity snapshot $E_T$.
3. Every new entry at boundary $T$ receives absolute USD notional:

$$\text{Notional}_i = \frac{E_T}{7}$$

4. Entry processing order across pairs does not alter sizing.
5. The position notional in USD remains constant throughout the trade until exit. Maximum aggregate gross leverage is 1.0 (100% when all 7 pairs are active).

### Daily Mark-to-Market and Primary Statistical Series
Because Candidate D trades run independently and overlap in calendar time, trade-level observations are cross-sectionally and serially dependent. The **primary statistical observation unit** is the **complete calendar-day portfolio net return series**:

1. **Valuation Timestamp:** Each calendar day $d$ covering $[T_d, T_d + 24\text{h})\text{ UTC}$ is valued at boundary $T_{d+1} = T_d + 24\text{h}$ ($00:00:00\text{Z}$ UTC).
2. **Separation of Execution Evidence and Mark Valuation:** Execution evidence at the 00h boundary governs transaction eligibility (entries and exits). In contrast, daily mark-to-market valuation is governed by valid completed Direct-D1 bar closes. Open positions are marked daily using the causal completed Direct-D1 close available at that valuation boundary. If the valid D1 close is unchanged (e.g. flat weekend row), mark return is naturally zero; if the valid D1 close changed, mark-to-market reflects that change regardless of 00h execution state.
3. **Daily Mark Accounting (No Double-Counting):**
   - **Entry Day ($d_{\text{entry}}$):** Position is marked from effective adverse entry fill price $P_{\text{entry}}$ to completed D1 close $\text{close}(i, d_{\text{entry}})$, minus entry-side commission deducted once in USD. Adverse spread and modeled slippage are fully incorporated in $P_{\text{entry}}$ and are never charged a second time in daily marks.
   - **Intermediate Holding Days ($d_{\text{entry}} < d < d_{\text{exit}}$):** Position is marked from prior completed D1 close $\text{close}(i, d-1)$ to current completed D1 close $\text{close}(i, d)$, with zero transaction costs.
   - **Exit Day ($d_{\text{exit}}$):** Position is marked from prior completed D1 close $\text{close}(i, d_{\text{exit}}-1)$ to effective adverse exit fill price $P_{\text{exit}}$, minus exit-side commission deducted once in USD. Adverse spread and modeled slippage are fully incorporated in $P_{\text{exit}}$ and are never charged a second time.
   - **Same-Boundary Re-entry:** Exit of the mature trade and entry of the new trade are accounted as separate economic legs with distinct fills and commissions.
4. **Calendar-Day Return:** Portfolio equity at the close of day $d$ is $E_d = E_{\text{cash}, d} + \sum_{i \in \text{open}} \text{MtM}_i(d)$, yielding:

$$R_{\text{portfolio}, d} = \frac{E_d - E_{d-1}}{E_{d-1}}$$

5. **Independent Headline and Stress Equity Paths:** Headline and 1.5x stress cost models produce separate, independent complete calendar-day equity curves ($E_{d, \text{headline}}$ and $E_{d, \text{stress}}$) and return series ($R_{d, \text{headline}}$ and $R_{d, \text{stress}}$). Headline and stress paths are never mixed.
6. **Equity Denominator:** Prior calendar-day closing equity $E_{d-1}$. This formulation contains zero look-ahead.

---

## 7. Cost Model

- **Adverse Bid/Ask Spread:** Fills determined from first valid tick quotes (`Ask` for buy, `Bid` for sell).
- **Baseline Slippage:** 0.2 pips per side (0.4 pips round-turn).
- **Commission:** USD 7.00 per standard lot (100,000 base units) round-turn (USD 3.50 per side).
- **Stress Multiplier (1.5x):** Adverse spread markup and slippage scaled by 1.5x. Commission is unscaled.
- **Pip Values:** `USDJPY` pip = 0.01; all other pairs pip = 0.0001.
- **Linear Valuation:** P&L converted to USD at adverse exit fill price.

---

## 8. Windows, Split, Purge, and 2024 Seal

### Research Intervals
- **Research input window:** `[2014-01-01T00:00:00Z, 2024-01-01T00:00:00Z)`
- **Train split:** `[2014-01-01T00:00:00Z, 2022-01-01T00:00:00Z)`
- **Validation embargo:** `2022-01-01T00:00:00Z`
- **Validation split:** `[2022-01-01T00:00:00Z, 2024-01-01T00:00:00Z)`
- **Sealed Test:** `2024-01-01T00:00:00Z+` (STRICTLY SEALED)

### Split Purge Without Future Leakage
To prevent future data leakage across split boundaries:
- For any candidate entry at boundary $T_{\text{entry}} < \text{split\_end}$:
  - The algorithm scans only boundaries strictly before $\text{split\_end}$ ($T \in [T_{\text{entry}} + 1\text{ day}, \text{split\_end})$).
  - If the 20th `AVAILABLE` boundary is not reached strictly before $\text{split\_end}$, the position is **PURGED** from that split's performance metrics.
  - The algorithm never reads, queries, or touches any execution evidence or bar data at $T \ge \text{split\_end}$.
- **Train Purge:** Evaluates boundaries strictly before `2022-01-01T00:00:00Z`.
- **Validation Purge & Seal Enforcement:** Evaluates boundaries strictly before `2024-01-01T00:00:00Z`. The 2024+ sealed partition is never accessed.

---

## 9. Metrics and Statistical Inference

### Metrics Computed
Every metric is reported separately for train and validation, headline and stress, calculated directly on the respective independent equity/return series:
- `trade_count`: Total and per-pair completed round-trip trades.
- `causal_non_entry_count`: Distribution by reason (`EMPTY_EVIDENCED`, `ABSENT_EVIDENCED`).
- `net_expectancy`: Mean completed-trade net return.
- `gross_expectancy`: Mean completed-trade gross return.
- `annualized_return`: Geometric growth of calendar equity curve raised to `365.2425 / calendar_days_in_split`, minus one.
- `Sharpe`: Mean divided by sample standard deviation (`ddof=1`) of the complete calendar-day net return series, times `sqrt(365.2425)`, risk-free zero.
- `MaxDD`: Maximum peak-to-trough decline of the compounded calendar equity curve ($0 \le \text{MaxDD} < 1$).
- `cost_drag`: Gross annualized return minus net annualized return, and gross expectancy minus net expectancy.
- `per_pair_contribution`: Cumulative net USD contribution per instrument.
- `yearly_net_return`: Calendar-year net returns for 2022 and 2023.

### Newey-West HAC Specification
Uncertainty estimation and lower confidence bound computation use an intercept-only Newey-West estimator on each complete calendar-day net return series (separately for headline and stress):
- **Kernel:** Bartlett weights
- **Automatic Bandwidth Lag:** $L = \lfloor 4 \cdot (N / 100)^{2/9} \rfloor$
- **Degrees of Freedom:** $N - 1$
- **Lower Confidence Bound:** $\text{LCB} = \text{mean} - t_{\text{crit}} \cdot \text{SE}_{\text{HAC}}$ where $t_{\text{crit}} = \text{scipy.stats.t.ppf}(0.99375, N - 1)$ (one-sided at $\alpha = 0.00625$).

---

## 10. Multiplicity and Adjusted Alpha

### Repository History Audit
A comprehensive audit of ADRs 0001 through 0010 establishes the following performance-testing history:
1. **Model A** (ADR 0001): Liquidity-sweep reversal — performance evaluated (NO-GO).
2. **Model B** (ADR 0001): Trend-pullback EMA — performance evaluated (NO-GO).
3. **Model C** (ADR 0001): Breakout-failure reversal — performance evaluated (NO-GO).
4. **Model D** (ADR 0001, 0002): FVG-retracement continuation — performance evaluated (NO-GO).
5. **Model E** (ADR 0003): Session opening-range breakout — performance evaluated (NO-GO).
6. **Model F** (ADR 0004): Single-instrument daily TSMOM — performance evaluated (NO-GO).
7. **TSMOM Portfolio** (ADR 0005): Multi-asset vol-scaled portfolio ("Mechanism #7") — performance evaluated (NO-GO).
8. **Candidate B** (ADR 0006, 0007): Public policy rate differential — rejected as INFEASIBLE before performance testing (0 performance tests consumed).
9. **Candidate C** (ADR 0008, 0009, 0010): Cross-sectional reversal — closed as NOT_EVALUABLE before performance testing (0 performance tests consumed).

### Sequential Test Index and Adjusted Alpha
- Exactly 7 prior mechanism families underwent performance evaluation.
- Candidate D is sequential performance-tested mechanism **Test 8** in repository research history.
- Applying a conservative Bonferroni family-wise adjustment across the $M = 8$ performance-evaluated mechanisms:

$$\alpha_{\text{adjusted}} = \frac{0.05}{8} = 0.00625 \quad (99.375\%\text{ one-sided confidence level})$$

Candidate D is frozen with adjusted alpha $\alpha = 0.00625$.

---

## 11. Deterministic P4 Decision Framework

### GO Decision
Decision string: `GO_TO_SEPARATELY_AUTHORIZED_SEALED_TEST`.
Requires all of the following conditions to be met simultaneously:
1. Train headline and stress net expectancy are strictly positive ($> 0$).
2. Validation headline and stress net return are strictly positive ($> 0$).
3. Validation headline and stress Sharpe are strictly positive ($> 0$).
4. Validation headline and stress net expectancy are strictly positive ($> 0$).
5. Validation headline and stress Newey-West HAC LCB are strictly positive ($> 0$) at $\alpha = 0.00625$.
6. Valid Max Drawdown: $0 \le \text{MaxDD} < 1$.
7. Non-negative Cost Drag: $\text{CostDrag} \ge 0$.
8. Yearly Consistency: Positive validation net return in both 2022 and 2023 separately under headline and stress.
9. Universe Breadth: At least 2 pairs exhibit strictly positive net contribution in both headline and stress.
10. Integrity: Clean worktree, ADR SHA-256 verified, zero look-ahead, zero future leakage, 2024+ untouched.

### NO_GO Decision
An evaluable measurement that fails one or more GO criteria.

### NOT_EVALUABLE Decision
Any data corruption, missing partition, seal breach, look-ahead, or structural failure that prevents complete valid execution.

---

## 12. Deterministic RunID Specification

The canonical `run_id` is computed as `canonical_sha256` over the following decision-affecting semantic inputs only:
- `schema`: `candidate_d_measurement_run.v1`
- `protocol_id`: `candidate_d_time_series_momentum.v1`
- `adr_sha256`: SHA-256 hash of the exact frozen bytes of ADR 0011
- `policy_id`: Canonical hash of frozen Candidate D policy object
- `code_commit`: Clean Git revision hash
- `datasets`: Ordered Direct-D1 dataset identities across all 7 pairs (provider, version, source reference, query fingerprints, content hashes, sealed bounds)
- `execution_manifest_id`: SHA-256 hash of verified 00h execution evidence manifest
- `execution_state_counts`: Distribution of execution states
- `universe`: Fixed canonical list of 7 pairs (`AUDUSD`, `EURUSD`, `GBPUSD`, `NZDUSD`, `USDCAD`, `USDCHF`, `USDJPY`)
- `windows`: Train `[2014-01-01, 2022-01-01)`, Validation `[2022-01-01, 2024-01-01)`, Sealed Test `2024-01-01+`
- `signal_definition`: 20-D1-interval log return requiring 21 valid close observations, native pair quoting orientation
- `causal_mapping`: Bar $t$ completing at $\text{ts\_open}(t) + 1\text{ day}$ 00:00:00Z UTC, entry at 00:00:00Z UTC
- `valid_signal_rows`: Direct-D1 structurally valid rows including zero-volume / flat rows
- `entry_evidence_rules`: `AVAILABLE` enters; `EMPTY_EVIDENCED`/`ABSENT_EVIDENCED` non-entry; others `NOT_EVALUABLE`
- `holding_exit_rule`: 20 `AVAILABLE` 00h sessions, 40-calendar-day cap
- `same_boundary_reentry`: Sequential exits -> signal -> entries
- `sizing_accounting_order`: Boundary snapshot $E_T$, fixed $E_T / 7$ notional per pair
- `cost_model`: Observed tick spread, 0.2 pip slippage per side (0.3 stress), USD 7.00/lot commission, 1.5x stress multiplier
- `primary_statistical_series`: Complete calendar-day portfolio net return series $R_{\text{portfolio}, d} = (E_d - E_{d-1})/E_{d-1}$
- `hac_specification`: Intercept-only Bartlett HAC, $L = \lfloor 4 \cdot (N/100)^{2/9} \rfloor$, Student's $t$ at $\alpha = 0.00625$
- `multiplicity_rule`: $M = 8$, $\alpha = 0.00625$
- `p4_decision_rule`: Exact 10-condition GO rule, fail-closed NO_GO / NOT_EVALUABLE contract

Dynamic runtime variables (filesystem paths, file modification times, network retrieval timestamps, retry counts, formatting strings, and execution results) are strictly excluded from the `run_id`.

Measurements from dirty worktrees are prohibited.

---

## 13. Authorization Boundary

After review and acceptance of this ADR, implementation of the Candidate D measurement module may begin. Executing the measurement remains a separate, explicit user action. Opening the 2024+ sealed test partition, ML training, demo/live execution, and real-money trading remain strictly unauthorized.
