# ADR 0014 — BIS Policy-Rate Evidence Acquisition Contract

- **Status:** Accepted — Frozen External Data Acquisition Contract
- **Research gate:** External evidence acquisition and normalization contract
- **Protocol family:** `post_test_8_research_family_v1` (Governance under ADR 0013)
- **Candidate E status:** NOT SELECTED (0 / 4 performance-test slots consumed)
- **Authoritative repository checkpoint:** `a47a65679121a1191dcbb532f2061542d7505730`

---

## 1. Purpose and Governance Authority

This ADR establishes the frozen, offline-verifiable data acquisition and normalization contract for central-bank policy-rate evidence from the Bank for International Settlements (BIS).

### Governance Invariants
- **Candidate E Status:** Candidate E remains **NOT SELECTED**.
- **Performance Test Accounting:** No performance-test slot is consumed by this data contract. The `post_test_8_research_family_v1` test count remains strictly **0 / 4**.
- **No Strategy Authorization:** This contract authorizes only the acquisition and normalization of external macroeconomic policy-rate data. It defines no trading strategy, ranking rule, directional signal, or entry/exit criteria, and authorizes no performance measurement.
- **Tenet:** `INFRASTRUCTURE CONFIDENCE != TRADING EDGE`. Verifiable external data acquisition does not establish the presence of an economic trading edge.

---

## 2. Authoritative Provider and Dataset Identification

The canonical numerical research evidence for central-bank policy rates is frozen exclusively to the Bank for International Settlements (BIS):

- **Institution:** Bank for International Settlements (BIS)
- **Dataset / Dataflow Identifier:** `BIS:WS_CBPOL(1.0)`
- **Dataset Title:** Central Bank Policy Rates (`WS_CBPOL`)
- **Frequency:** Daily (`D`)

### Frozen Currency Series Mapping
The dataset contract covers exactly the eight central banks corresponding to the seven canonical FXLab currency pairs:

| Currency | Central Bank Authority | Canonical BIS Series Key | Rate Instrument Concept |
| :--- | :--- | :--- | :--- |
| **USD** | Federal Reserve (FOMC) | `D.US` | Federal Funds Target Rate (midpoint of target range) |
| **AUD** | Reserve Bank of Australia (RBA) | `D.AU` | Cash Rate Target |
| **EUR** | European Central Bank (ECB) | `D.XM` | Main Refinancing Operations (MRO) fixed rate |
| **GBP** | Bank of England (BoE / MPC) | `D.GB` | Official Bank Rate |
| **NZD** | Reserve Bank of New Zealand (RBNZ) | `D.NZ` | Official Cash Rate (OCR) |
| **CAD** | Bank of Canada (BoC) | `D.CA` | Target for the Overnight Rate |
| **CHF** | Swiss National Bank (SNB) | `D.CH` | SNB Policy Rate (3M CHF LIBOR target midpoint prior to June 2019) |
| **JPY** | Bank of Japan (BoJ) | `D.JP` | Uncollateralized Overnight Call Rate target / Policy Rate |

National central-bank publications may be consulted for institutional metadata verification, but the canonical numerical series under this contract is BIS `WS_CBPOL`. Automatic substitution of alternative third-party providers is prohibited.

---

## 3. EUR Series Specification

For the Euro area (`EUR`), the series is frozen strictly to canonical **`BIS D.XM`**:

- **Historical Definition:** Throughout the 2014-01-01 through 2023-12-31 research interval, official BIS documentation identifies the recorded rate for `D.XM` as the ECB Main Refinancing Operations (MRO) fixed rate.
- **Anti-Tuning Invariant:** No post-hoc switching between the MRO rate and the Deposit Facility Rate (DFR) or Marginal Lending Facility rate is permitted. The canonical BIS `D.XM` series is invariant across train and validation splits.

---

## 4. Research Window and Sealed-Test Boundary

The evidence acquisition interval is strictly bounded:

- **Start Bound (Inclusive):** `2014-01-01T00:00:00Z` (`2014-01-01`)
- **End Bound (Exclusive):** `2024-01-01T00:00:00Z` (`2024-01-01`)

### Sealed-Test Boundary Protection
- **Bounded Ingestion:** The acquisition mechanism must explicitly request the bounded date interval if supported by the source interface (e.g., SDMX query parameter `startPeriod=2014-01-01&endPeriod=2023-12-31`).
- **No 2024+ Ingestion:** No observation with an effective date on or after `2024-01-01T00:00:00Z` may enter the acquired research artifact or normalized dataset.
- **Fail-Closed Verification:** If the source API or download artifact cannot verifiably prove compliance with the requested time bounds, ingestion terminates immediately as a failure.

---

## 5. Point-in-Time Causal Contract

Central-bank policy rates are administrative decisions published at discrete announcement times with designated effective dates.

### Decision-Boundary Causal Eligibility
At any historical FXLab decision boundary $T = \text{00:00:00 UTC}$ on calendar date $D$:
1. A policy rate observation is causally eligible for use if and only if its official effective date is **strictly earlier than $T$** (i.e. effective date $\le D - 1\text{ calendar day}$).
2. A policy rate decision announced or becoming effective during calendar date $D$ must **not** influence the decision at $D\text{ 00:00:00 UTC}$.
3. The earliest boundary at which a newly effective policy rate may be observed is the subsequent 00:00:00 UTC boundary strictly following its official effective date (typically $(D+1)\text{ 00:00:00 UTC}$).
4. If source metadata cannot conclusively establish the causal eligibility of an observation relative to boundary $T$, the pipeline fails closed. Intraday announcement timestamps must never be fabricated from daily date strings.

---

## 6. Rate Persistence vs. Missing Data

### Administrative Step-Function Property (`RATE_PERSISTS`)
Policy interest rates remain constant between administrative central-bank actions:
- On non-meeting days, weekends, and official public holidays, the policy rate remains valid and is carried forward as an active administrative state (`RATE_PERSISTS`).
- Carrying forward an established effective policy rate is an accurate representation of the institutional reality, not statistical imputation or interpolation.

### Unhandled Missing Data (`MISSING_OR_UNKNOWN`)
The following conditions represent structural evidence failures, classified as `MISSING_OR_UNKNOWN`:
- Absence of a valid prior effective rate at the beginning of the research window;
- Unexplained date gaps within a central bank's published series;
- Non-numeric, non-finite, NaN, or corrupted observations;
- Ambiguous date ordering or conflicting duplicate records;
- Unresolved breaks in series metadata.

Any occurrence of `MISSING_OR_UNKNOWN` fails closed immediately as an evidence error. Fabricating, linear-interpolating, or synthesizing policy rates is strictly forbidden.

---

## 7. Economic Semantics Disavowal

$$\text{POLICY\_RATE\_DIFFERENTIAL} \neq \text{EXACT\_TRADABLE\_FX\_CARRY}$$

This dataset captures central-bank monetary-policy target rates only. It must **not** be described or modeled as exact tradable FX carry.

Specifically, policy-rate differentials do not reflect:
1. Interbank unsecured funding spreads (e.g. LIBOR / SOFR / €STR spreads over target rates);
2. Tradable spot-next and forward swap points;
3. Covered Interest Parity (CIP) cross-currency basis deviations;
4. Commercial broker rollover financing debits/credits;
5. Transaction costs or realized carry returns.

Any future research using this evidence must be explicitly designated as a **monetary-policy-rate differential** hypothesis.

---

## 8. Raw Evidence Provenance Specification

Future acquisition must record an immutable raw artifact capturing complete provenance:

### Required Raw Provenance Fields
- `source_institution`: `Bank for International Settlements (BIS)`
- `dataset_identifier`: `BIS:WS_CBPOL(1.0)`
- `frequency`: `D` (Daily)
- `requested_series`: `["D.US", "D.AU", "D.XM", "D.GB", "D.NZ", "D.CA", "D.CH", "D.JP"]`
- `start_inclusive`: `2014-01-01`
- `end_exclusive`: `2024-01-01`
- `format`: `sdmx-csv` or `sdmx-xml`
- `raw_byte_count`: Exact integer byte length
- `raw_sha256`: SHA-256 hash over exact downloaded payload

Machine-specific local filesystem paths, file modification times (`mtime`), and network retrieval timestamps must be recorded only as transient audit metadata and must never alter the cryptographic content identity.

---

## 9. Normalized Evidence Contract (`bis_cbpol_daily_v1`)

The normalized dataset identity is frozen as schema version **`bis_cbpol_daily_v1`**.

### Normalization Requirements
1. **Scope:** Parses exactly the eight frozen daily series (`D.US`, `D.AU`, `D.XM`, `D.GB`, `D.NZ`, `D.CA`, `D.CH`, `D.JP`).
2. **Date Alignment:** Preserves UTC calendar-day dates (`YYYY-MM-DD`).
3. **Numeric Integrity:** Retains source decimal rate values without scaling, rounding, or strategy-dependent transformations.
4. **Validation:** Rejects non-finite values, duplicate contradictory records, and unmapped currency codes.
5. **Sealed Boundary Enforcement:** Accepts only evidence acquired under the bounded source request defined in Section 4 and verifies every parsed observation satisfies 2014-01-01 <= date < 2024-01-01. Any observation outside the frozen interval, or inability to prove that the source request itself was bounded, causes normalization to fail closed. Post-download truncation must never be used to legitimize an unrestricted/latest acquisition.
6. **State Tracking:** Explicitly distinguishes `RATE_PERSISTS` from `MISSING_OR_UNKNOWN`.
7. **Deterministic Fingerprint:** Produces a deterministic content hash over the canonical tabular data payload, invariant to machine paths and timestamps.

---

## 10. Mathematical Pair Orientation (Metadata Reference Only)

For each canonical currency pair $\text{BASE}/\text{QUOTE}$, the mathematical policy-rate differential is defined as:

$$\Delta r_{\text{BASE}/\text{QUOTE}} = r_{\text{BASE}} - r_{\text{QUOTE}}$$

| Currency Pair | Base Currency | Quote Currency | Mathematical Differential Formula |
| :--- | :--- | :--- | :--- |
| **AUDUSD** | AUD | USD | $r_{\text{AUD}} - r_{\text{USD}}$ |
| **EURUSD** | EUR | USD | $r_{\text{EUR}} - r_{\text{USD}}$ |
| **GBPUSD** | GBP | USD | $r_{\text{GBP}} - r_{\text{USD}}$ |
| **NZDUSD** | NZD | USD | $r_{\text{NZD}} - r_{\text{USD}}$ |
| **USDCAD** | USD | CAD | $r_{\text{USD}} - r_{\text{CAD}}$ |
| **USDCHF** | USD | CHF | $r_{\text{USD}} - r_{\text{CHF}}$ |
| **USDJPY** | USD | JPY | $r_{\text{USD}} - r_{\text{JPY}}$ |

> [!IMPORTANT]
> **MATHEMATICAL DIFFERENTIAL ORIENTATION ONLY.**
> The formulas above define algebraic currency differentials only. They do not define trading direction (long/short), entry thresholds, rebalance schedules, holding periods, or portfolio weights.

---

## 11. Authorization and Governance Boundary

- **Candidate E Selected:** `NO`
- **Performance-Test Slot Consumed:** `NO` (Budget remains `0 / 4` under ADR 0013)
- **2024+ FX Test Window:** `STRICTLY SEALED`
- **Machine Learning Authorization:** `NO`
- **Demo / Live / Real-Money Trading Authorization:** `NO`

Freezing this external evidence contract authorizes data pipeline construction and offline artifact verification only. Candidate selection and strategy preregistration remain distinct future governance milestones.
