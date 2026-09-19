# ADR 0015 — BIS Policy-Rate Missing-Observation Semantics and State-Persistence Amendment

- **Status:** Draft / Proposed — External Evidence Contract Amendment
- **Research gate:** External evidence acquisition and normalization contract amendment
- **Protocol family:** `post_test_8_research_family_v1` (Governance under ADR 0013)
- **Candidate E status:** NOT SELECTED (0 / 4 performance-test slots consumed)
- **Amends:** [ADR 0014 (Section 6 & Section 9)](file:///E:/build/codex-workspaces/fxlab-codex-8f7e5f0/docs/adr/0014-bis-policy-rate-evidence-acquisition-contract.md)
- **Authoritative repository checkpoint:** `21d00e66065b76badffe4e4c3cdc5d0684e9d9be`

---

## 1. Purpose and Governance Context

This ADR establishes the formal amendment to the BIS policy-rate normalization contract defined in [ADR 0014](file:///E:/build/codex-workspaces/fxlab-codex-8f7e5f0/docs/adr/0014-bis-policy-rate-evidence-acquisition-contract.md). It specifies the deterministic representation of central-bank policy-rate **state persistence** across explicitly missing BIS daily calendar observations (`OBS_VALUE=NaN`, `OBS_STATUS=M`) where an established prior policy rate was legally and institutionally in force.

### Governance Invariants
- **ADR 0014 Byte Preservation:** ADR 0014 remains historically frozen and unmodified. This document acts as an explicit amendment to Sections 6 and 9 of ADR 0014.
- **Candidate E Status:** Candidate E remains **NOT SELECTED**.
- **Performance Test Accounting:** The `post_test_8_research_family_v1` test count remains strictly **0 / 4**.
- **No Strategy Authorization:** This contract authorizes only external data normalization semantics. It defines no trading signals, portfolio weights, or execution rules, and authorizes no performance evaluation.
- **Sealed Test Boundary:** The research interval remains strictly bounded to $2014\text{-}01\text{-}01 \le \text{date} < 2024\text{-}01\text{-}01$. 2024+ FX data remains strictly sealed.

---

## 2. Forensic Investigation and Acquisition History

### Canonical Acquisition Attempts Record
1. **Canonical Acquisition Attempt #1:**
   - **Result:** FAILED during normalization on `ValueError: non-finite rate value: NaN`.
   - **Diagnostic Limitation:** Diagnostic context was not preserved by the original parser, preventing immediate identification of the offending series and date.
   - **Transactional Integrity:** Zero raw or normalized canonical artifacts were published.
2. **Diagnostic Error Context Implementation:**
   - Introduced structured exception `BisObservationValidationError` capturing `series_key`, `time_period`, `raw_obs_value`, `obs_status`, `obs_conf`, and `obs_pre_break` while preserving fail-closed rejection.
3. **Canonical Acquisition Attempt #2:**
   - **Result:** FAILED, fail-closed with structured diagnostic context.
   - **Exact Offending Source Observation:**
     - `series_key`: `D.CA`
     - `time_period`: `2014-01-04`
     - `raw_obs_value`: `NaN`
     - `obs_status`: `M`
     - `obs_conf`: `F`
   - **Transactional Integrity:** Zero canonical artifacts were published in `E:\jarvis-data\fxlab-research\bis-policy-rates-v1`.

### Authoritative BIS Diagnostic Verification
A micro-diagnostic query covering `D.CA` over the interval `2014-01-02` through `2014-01-06` returned:
- `2014-01-02` (Thursday): `OBS_VALUE="1"`, `OBS_STATUS="A"`, `OBS_CONF="F"`
- `2014-01-03` (Friday): `OBS_VALUE="1"`, `OBS_STATUS="A"`, `OBS_CONF="F"`
- `2014-01-04` (Saturday): `OBS_VALUE="NaN"`, `OBS_STATUS="M"`, `OBS_CONF="F"`
- `2014-01-05` (Sunday): `OBS_VALUE="NaN"`, `OBS_STATUS="M"`, `OBS_CONF="F"`
- `2014-01-06` (Monday): `OBS_VALUE="1"`, `OBS_STATUS="A"`, `OBS_CONF="F"`

### Authoritative Semantics
- **`OBS_STATUS=M`:** SDMX cross-domain code for *Missing value* (data point missing or not published on non-working calendar days).
- **`OBS_CONF=F`:** SDMX cross-domain code for *Free* (unrestricted public access).
- **Institutional Context:** Bank of Canada policy rate targets are administrative decisions that remain continuously in force until superseded by a subsequent decision. The 1.00% target established on 2010-09-08 remained legally and operationally in force across weekend non-business days (2014-01-04 and 2014-01-05).

---

## 3. State Persistence vs. Statistical Imputation

A strict distinction is maintained between statistical imputation and institutional state persistence:

$$\text{STATISTICAL IMPUTATION} \neq \text{INSTITUTIONAL STATE PERSISTENCE}$$

- **Statistical Imputation (PROHIBITED):** Synthesizing, estimating, linear-interpolating, or regressing an unknown market price or macroeconomic value.
- **State Persistence (PERMITTED):** Carrying forward a discrete administrative policy-rate state that was explicitly established by central-bank authority and remained legally in force until explicitly altered by a subsequent administrative action.

---

## 4. Required Raw Observation Model

The raw evidence representation must strictly preserve the authentic source payload semantics without rewriting missing records into synthetic numeric reports:

1. **Classification:** Every raw observation is distinctly classified as either:
   - `NUMERIC_OBSERVATION`: Source payload provides a valid finite decimal rate string (e.g. `OBS_VALUE="1"`, `OBS_STATUS="A"`).
   - `MISSING_OBSERVATION`: Source payload provides a non-numeric/missing indicator (e.g. `OBS_VALUE="NaN"`, `OBS_STATUS="M"`).
2. **Prohibition of In-Place Raw Mutation:** Raw observations with `OBS_VALUE="NaN"` and `OBS_STATUS="M"` must **never** be rewritten in raw artifacts as though the BIS reported a numeric rate on that date.
3. **Full Provenance Preservation:** All raw attributes (`TIME_PERIOD`, `OBS_VALUE`, `OBS_STATUS`, `OBS_CONF`, `OBS_PRE_BREAK` if present) must remain preserved and auditable.

---

## 5. Derived Policy-Rate State Model

In the normalized representation, a derived state field is constructed separately from the raw observation:

### Conceptual Representation
- `source_observation_kind`: `NUMERIC` | `MISSING`
- `source_obs_value`: Raw string representation (e.g. `"1"`, `"NaN"`)
- `source_obs_status`: Raw status code (e.g. `"A"`, `"M"`)
- `policy_rate_state`: Decimal policy rate in force (e.g. `1.00`)
- `policy_rate_state_origin`: `OBSERVED` | `PERSISTED`
- `source_state_date`: Calendar date on which the active `policy_rate_state` was source-observed (e.g. `2014-01-03`)

### Persistence Criteria
A missing source observation inherits the immediately preceding established policy-rate state if and only if all of the following conditions are met:
1. `OBS_VALUE` is non-finite / `"NaN"`;
2. `OBS_STATUS` is strictly `"M"` (the authoritative missing status code);
3. A strictly earlier, valid, finite numeric observation exists within the **same series** (`series_key`);
4. No contradictory observation exists for the same calendar date;
5. No source metadata explicitly contradicts persistence or indicates an unresolved structural transition;
6. The observation falls strictly within the sealed research boundary $[2014\text{-}01\text{-}01, 2024\text{-}01\text{-}01)$.

---

## 6. Strict Fail-Closed Boundaries

The normalization pipeline must immediately fail closed without writing canonical artifacts in any of the following circumstances:

1. **Leading Missing Observations:** A missing observation (`OBS_STATUS="M"`) occurs before any valid finite numeric state has been established for that series (e.g. series starts on a weekend/holiday with no prior rate).
2. **Unrecognized Status Codes:** `OBS_VALUE` is non-finite or `"NaN"` but `OBS_STATUS` is not `"M"` (e.g. non-finite with status `"A"`, `"ND"`, or unknown status).
3. **Series Discontinuity / Ambiguity:** Duplicate observations on the same date with conflicting values or ambiguous date ordering.
4. **Structural / Instrument Transitions:** Authoritative source metadata identifies a framework, instrument, or series break that makes continuation of the prior policy rate ambiguous.
5. **No Arbitrary Gap Assumptions:** No arbitrary maximum missing-gap limit (such as 10 days) is invented. If a future calendar-gap constraint is introduced, it must be supported by independent domain justification before being frozen.
6. **Boundary Violations:** Any observation date $< 2014\text{-}01\text{-}01$ or $\ge 2024\text{-}01\text{-}01$.

---

## 7. Structural Breaks and `OBS_PRE_BREAK` Handling

This amendment introduces no synthetic or unverified interpretations of the SDMX `OBS_PRE_BREAK` attribute:

- **Break Ambiguity Rule:** If source metadata or series documentation indicates an institutional framework change, operational target transition, or instrument break that makes carrying forward the previous numerical rate ambiguous, state persistence across the break must fail closed.
- **Explicit Semantics Required:** `OBS_PRE_BREAK` must not trigger automatic numeric adjustments unless its exact authoritative source semantics for that specific central bank are established by formal governance.

---

## 8. Point-in-Time Information Firewall

State persistence addresses solely the institutional reality of what central-bank policy rate was legally in force. It **does not** establish point-in-time causal availability for trading decisions:

$$\text{INSTITUTIONAL STATE IN FORCE} \neq \text{CAUSALLY AVAILABLE AT 00:00:00 UTC}$$

1. **`POINT_IN_TIME_STATUS` Remains `UNRESOLVED`:** BIS SDMX daily feeds do not provide publication release timestamps, intraday announcement times, or publication latency metadata.
2. **Strategy Firewall:** Persisted or directly observed policy-rate states must **not** be consumed by Candidate E strategy logic, signal generators, or backtests until an independent, explicit point-in-time causal governance rule is established.
3. **No Historical Signal Generation:** No directional signal, differential ranking, or trading decision is authorized by this amendment.

---

## 9. Summary of Specific Amendments to ADR 0014

| ADR 0014 Section | Previous Rule in ADR 0014 | Amended Rule under ADR 0015 |
| :--- | :--- | :--- |
| **Section 6 (Unhandled Missing Data)** | Rejected all non-numeric / NaN records indiscriminately as `MISSING_OR_UNKNOWN`. | Distinguishes `OBS_STATUS="M"` on non-business calendar days as eligible for series-local `policy_rate_state` persistence when preceded by a valid finite numeric state. Non-`M` NaNs continue to fail closed. |
| **Section 9 (Normalization Contract)** | Failed closed on any raw `"NaN"` observation during initial decimal parsing. | Preserves raw `"NaN"` under `source_observation_kind="MISSING"`, while deriving `policy_rate_state` from the prior established state with `policy_rate_state_origin="PERSISTED"`. |

---

## 10. Authorization and Governance Status

- **Candidate E Selected:** `NO`
- **Performance-Test Slot Consumed:** `0 / 4` (Budget under ADR 0013 remains strictly 0)
- **2024+ FX Test Window:** `STRICTLY SEALED`
- **Code Changes Made:** `NONE`
- **Canonical Acquisition Run:** `NO`
- **Trading Execution / ML Authorization:** `NO`
