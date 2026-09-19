# ADR 0013 — Post-Test-8 Research Multiplicity Governance

- **Status:** Accepted — Prospective Research Governance
- **Research gate:** Multiplicity policy and statistical budget freeze
- **Protocol family:** `post_test_8_research_family_v1`
- **Supersedes:** Closes original eight-test research family permanently
- **Authoritative repository checkpoint:** `dc083aa22aa7cf0cf8008eb173af697b4bfa464a`

---

## 1. Executive Summary and Historical Boundary

This ADR permanently closes the original FXLab research family ($M = 8$, family-wise $\alpha = 0.05$, per-test $\alpha = 0.00625$) and establishes prospective statistical governance for future hypothesis testing before any new candidate (e.g., Candidate E) is selected, preregistered, or evaluated.

### Historical Performance Test Audit (Tests 1–8 Consumed)
Authoritative repository ADRs 0001 through 0012 record exactly eight completed performance evaluations in the original research family:

1. **Model A** (ADR 0001): Liquidity-sweep reversal — performance evaluated (`NO-GO`).
2. **Model B** (ADR 0001): Trend-pullback naive baseline — performance evaluated (`NO-GO`).
3. **Model C** (ADR 0001): Breakout-failure reversal — performance evaluated (`NO-GO`).
4. **Model D** (ADR 0001, ADR 0002): FVG-retracement continuation — performance evaluated (`NO-GO`).
5. **Model E** (ADR 0003): Session opening-range breakout — performance evaluated (`NO-GO`).
6. **Model F** (ADR 0004): Single-instrument daily TSMOM — performance evaluated (`NO-GO`).
7. **TSMOM Portfolio** (ADR 0005): Multi-asset vol-scaled portfolio ("Mechanism #7") — performance evaluated (`NO-GO`).
8. **Candidate D** (ADR 0011, ADR 0012): G10 time-series trend momentum ("Test 8") — performance evaluated (`NO_GO`).

### Pre-Performance Exclusions
- **Candidate B** (ADR 0006, ADR 0007): Public policy rate differential — rejected as `INFEASIBLE` before performance testing (0 performance tests consumed).
- **Candidate C** (ADR 0008, ADR 0009, ADR 0010): Cross-sectional reversal — closed as `NOT_EVALUABLE` before valid performance evaluation (0 performance tests consumed).

**Conclusion:** The original family consumed exactly 8 of 8 allocated performance-test slots. The original $M = 8$ family is fully exhausted. No existing rule authorizes a "Test 9" under the original family.

---

## 2. Prospective Future Research Family (`post_test_8_research_family_v1`)

To enable disciplined future empirical research without unbounded data snooping, a new prospective finite-family research budget is established:

- **Family ID:** `post_test_8_research_family_v1`
- **Maximum Performance Tests ($M$):** `4`
- **Family-Wise Error Rate ($\alpha_{\text{family}}$):** `0.05`
- **Multiplicity Correction Method:** Bonferroni inequality
- **Per-Test Adjusted Significance Level ($\alpha_{\text{per-test}}$):**

$$\alpha_{\text{per-test}} = \frac{\alpha_{\text{family}}}{M} = \frac{0.05}{4} = 0.0125 \quad (98.75\%\text{ one-sided confidence level})$$

- **Critical HAC Bound:** For sample size $N$ calendar days, the Newey-West lower confidence bound (LCB) uses $t_{\text{crit}} = \text{scipy.stats.t.ppf}(0.9875, N - 1)$.

### Research-Budget Conservation Rationale
The allocation of exactly 4 test slots is justified strictly by research-budget conservation:
1. **Finite Search Budget:** Prevents open-ended, sequential hypothesis grinding on the historical dataset.
2. **Auditability:** Enforces clear, integer-indexed tracking of every empirical evaluation attempt.
3. **Hypothesis Selectivity:** Forces high ex-ante theoretical and economic filtering before committing a scarce test slot.
4. **Transparent Accounting:** Provides a simple, rigid Bonferroni threshold without complex adaptive spending rules.

### Cumulative Search Transparency
The 4-test family is a distinct, prospectively declared research family. It does **not** reset, erase, invalidate, or reinterpret the 8 historical tests. Any future research publication or report must transparently disclose that these 4 tests follow the 8 prior exhausted tests on the historical dataset. Claims must never be framed as if only 4 total strategies were ever evaluated.

---

## 3. Test Consumption and Anti-Loophole Rules

### When a Test Slot is Consumed
A performance-test slot within `post_test_8_research_family_v1` is consumed if and only if:
- A candidate protocol is prospectively preregistered in an accepted ADR;
- It reaches **valid performance evaluation** on the reused 2014–2023 research design; and
- Its preregistered performance statistics (e.g. net expectancy, annualized return, Sharpe ratio, drawdowns, and HAC bounds) are inspected and evaluated as the candidate's authoritative research result.

### Pre-Performance Non-Consumption and Intermediate Calculations
Mere execution of code, generation of intermediate/provisional calculations, feasibility checks, integrity checks, or a structural `NOT_EVALUABLE` termination before valid performance evaluation does **not** by itself consume a performance-test slot. Specifically, a proposed candidate that terminates prior to valid performance evaluation due to:
- Infeasible data/evidence coverage;
- Causal or look-ahead invalidity discovered during pre-measurement audit;
- Data corruption, missing execution partitions, or decompressed tick errors; or
- Structural `NOT_EVALUABLE` status where train/validation performance metrics remain null or uncomputed,

does not consume one of the 4 performance-test slots.

### Blocking the `NOT_EVALUABLE` Iteration Loophole
To prevent `NOT_EVALUABLE` or pre-performance failure from serving as a loophole for unpenalized trial-and-error optimization or unlimited retries:
1. **Permanent Archival:** Every attempted candidate (including infeasible or non-evaluable attempts) must remain permanently recorded in a dedicated ADR.
2. **No Interactive Tuning:** `NOT_EVALUABLE` does not permit iterative tweaking or retries; the same frozen protocol cannot simply be rerun seeking evaluability.
3. **Distinct Identity & Governance Review:** Any material protocol or hypothesis revision requires a new prospective identity, formal pre-freeze consistency review in a new ADR, and explicit governance approval before any subsequent performance evaluation attempt.
4. **No Slot Recycling:** Once a valid performance evaluation has occurred, that test slot and its allocated significance level are permanently consumed and cannot be recycled.

---

## 4. Strict Anti-Recycling and Parameter Invariance Rules

The statistical threshold $\alpha = 0.0125$ is immutable across the lifecycle of `post_test_8_research_family_v1`:

- **No Alpha Recycling:** A failed hypothesis (`NO_GO`) or terminated test does not return its allocated $\alpha = 0.0125$ to the budget.
- **No Alpha Transfer:** Unused significance from one test cannot be added to or pooled with another test.
- **No Order-Dependent Thresholds:** The testing order of candidates cannot alter the required $\alpha = 0.0125$ cutoff.
- **No Post-Hoc Family Expansion:** The family capacity $M = 4$ cannot be expanded after observing test outcomes.
- **No Failure-Driven Relaxation:** Consecutive failures cannot be used to justify relaxing $\alpha$ or expanding the hypothesis space.

---

## 5. Candidate Selection Boundary

This governance ADR is established strictly at the meta-methodological level. It freezes the statistical framework **before** any specific candidate (such as Candidate E) is selected or evaluated.

Consequently, this ADR explicitly refrains from:
- Selecting or prioritizing any candidate mechanism (e.g., Calendar Flow, Defensive Volatility, or any other concept);
- Specifying parameters, indicator horizons, or trading rules for future candidates;
- Ranking potential candidates or asserting expected returns, Sharpe ratios, or probability of success.

Candidate selection, specification, and preregistration must occur in separate subsequent ADRs adhering to this framework.

---

## 6. Validation Data Reuse and Independence Disclosure

The 2022–2023 validation dataset has been repeatedly queried during prior research evaluations (ADRs 0001–0012).

### Methodological Implications
1. **Compromised Pristine Status:** The 2022–2023 validation window is not an untouched, out-of-sample dataset.
2. **Limits of Multiplicity Control:** While Bonferroni adjustment bounds family-wise Type I error under nominal testing assumptions, statistical corrections cannot restore the physical independence of reused data.
3. **Mandatory Disclosure:** All future candidate evaluations must explicitly disclose the repeated prior reuse of the 2022–2023 validation split.

### Inviolability of the 2024+ Sealed Test Window
- The `2024-01-01T00:00:00Z+` partition remains strictly sealed and unread.
- No data, metadata, timestamps, hashes, or execution states from 2024+ may be requested or inspected during candidate development or P4 evaluation.
- The sealed partition may only be accessed following a formal, separately authorized decision.

---

## 7. Decision Semantics and Authorization Boundaries

### Scope of a P4 `GO` Outcome
If a candidate evaluated under `post_test_8_research_family_v1` satisfies all preregistered P4 criteria, its valid decision string is strictly:

`GO_TO_SEPARATELY_AUTHORIZED_SEALED_TEST`

A `GO` decision confirms only that the candidate demonstrated statistical viability on the in-sample and validation partitions under preregistered criteria.

A `GO` decision **does NOT authorize**:
- Unsealing or opening the 2024+ test partition;
- Training or fitting machine learning models;
- Deploying algorithms to MT5 demo/live environments;
- Live trading or real-money operations.

Each subsequent phase requires distinct governance approval and explicit authorization.

---

## 8. Core Research Principles

All future research within `post_test_8_research_family_v1` remains subject to FXLab foundational principles:

- **INFRASTRUCTURE CONFIDENCE != TRADING EDGE:** Passing data integrity checks, execution-evidence parsing, or Gate A/B1/B2 operational hurdles provides zero evidence of an economic trading edge.
- **NO EDGE and NOT_EVALUABLE are standard research outcomes:** Empirical failure must be accepted without post-hoc rationalization.
- **AI RESEARCH != PERMISSION TO TRADE:** Systematic exploration does not grant authority for live capital risk.
