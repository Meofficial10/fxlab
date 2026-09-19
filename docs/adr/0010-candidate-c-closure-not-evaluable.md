# ADR 0010 — Candidate C closure: NOT_EVALUABLE under frozen protocols

- **Status:** Accepted (2026-09-19)
- **Research gate:** P4 statistical evaluation closure
- **Candidate:** Candidate C — cross-sectional five-day FX reversal
- **Protocols evaluated:** `candidate_c_cross_sectional_reversal.v1`, `candidate_c_cross_sectional_reversal.v2`
- **Supersedes:** Nothing; records final closure of Candidate C without modifying ADR 0008 or ADR 0009
- **Candidate C research status:** **CLOSED / NOT_EVALUABLE**
- **Candidate C edge status:** **UNDETERMINED**

## 1. Context and Master Principles

Candidate C was preregistered to test a cross-sectional five-day reversal hypothesis across seven canonical currency pairs using Dukascopy Direct-D1 bar data and hourly execution evidence from 2014 through 2023.

This record formalizes the permanent closure of Candidate C under FXLab's core scientific governance principles:

- **INFRASTRUCTURE CONFIDENCE != TRADING EDGE.** Verified data pipelines and compliant execution harnesses do not constitute an empirical edge.
- **AI RESEARCH != PERMISSION TO TRADE.** Research accounting operates under strict fail-closed criteria; inconclusive or unmeasured candidates never receive execution authority.
- **NO EDGE and NOT_EVALUABLE are acceptable research outcomes.** Preserving protocol integrity and auditability takes absolute precedence over obtaining evaluable metrics through post-hoc modifications.

Candidate C did **NOT** earn P4 GO. It did **NOT** authorize machine learning, did **NOT** authorize opening the sealed 2024+ dataset, did **NOT** authorize demo execution or strategy deployment, and did **NOT** authorize real-money trading. The 2024+ period remains sealed and untouched.

---

## 2. Candidate C v1 Historical Record

Candidate C v1 was preregistered in [ADR 0008](0008-candidate-c-cross-sectional-reversal-preregistration.md).

- **Protocol:** `candidate_c_cross_sectional_reversal.v1`
- **Preregistration ADR:** ADR 0008
- **Decision:** `NOT_EVALUABLE`
- **Run ID:** `88aed79769b0a7f38031744e678cd8996391a3ffdb94290e515417f1e5503910`
- **Result ID:** `dc08c6baedbbdbf652bca9e069f7349fca841366b1fd476656167663fc6ed9d7`
- **Reason:** `exit_empty_evidenced`

### Finding
The fixed single-day holding rule (`exit = entry + 1 calendar day`) required exit on the immediately following calendar day 00h partition. Valid Friday entry cohorts (e.g., `2014-01-10T00:00:00Z`) required exits on Saturday `2014-01-11T00:00:00Z`, where upstream evidence was genuinely `EMPTY_EVIDENCED` due to scheduled weekend market closure. Under ADR 0008, this structural absence of Saturday prices correctly failed closed as `NOT_EVALUABLE`.

---

## 3. Candidate C v2 Historical Record & Forensic Diagnostic

Candidate C v2 was prospectively preregistered in [ADR 0009](0009-candidate-c-v2-tradable-session-exit-preregistration.md) to define a multi-day tradable-session exit state machine searching up to 7 calendar days for the next all-seven `AVAILABLE` boundary while skipping all-seven non-trading closure boundaries (`{ABSENT_EVIDENCED, EMPTY_EVIDENCED}`).

- **Protocol:** `candidate_c_cross_sectional_reversal.v2`
- **Preregistration ADR:** ADR 0009
- **Implementation commit:** `e48718d0aacd6309f6a848dbbb9d108d14a94cda`
- **Decision:** `NOT_EVALUABLE`
- **Run ID:** `ed8a8e5f4823e81171208094860af3d02bd370ad438f01de4a7358b38e6b32f1`
- **Result ID:** `3a845829701fc62be4cdccb36de9278284442b51ff3a19a7bf631f65aaa01e6c`
- **Policy ID:** `bdbdebd20a7191c6d7cd160eeb710342410445c0e22d9b46fb80ee18d2bbe33b`
- **Reason:** `exit_empty_evidenced`

### Forensic Diagnostic Trace
- A valid Friday entry occurred on `2023-12-22T00:00:00Z` with all 7 pairs `AVAILABLE`.
- **Boundary 1 (`2023-12-23T00:00:00Z` - Saturday):** All 7 pairs were `EMPTY_EVIDENCED` (evidenced non-execution closure) and were correctly skipped per ADR 0009 Section 3.2 Rule 2.
- **Boundary 2 (`2023-12-24T00:00:00Z` - Sunday):** All 7 pairs were `EMPTY_EVIDENCED` (evidenced non-execution closure) and were correctly skipped per ADR 0009 Section 3.2 Rule 2.
- **Boundary 3 (`2023-12-25T00:00:00Z` - Monday / Christmas Day):** 6 pairs were `AVAILABLE` (AUDUSD, EURUSD, GBPUSD, USDCAD, USDCHF, USDJPY) and 1 pair (`NZDUSD`) was `EMPTY_EVIDENCED`.
- Under ADR 0009 Section 3.2 Rule 3, any boundary exhibiting partial universe availability ("any mixture containing one or more AVAILABLE records without all seven being AVAILABLE") is an unverifiable partial boundary that strictly mandates failing closed as `NOT_EVALUABLE`. It cannot be treated as closure evidence and cannot be skipped.

### Diagnostic Classification
- **Forensic classification:** `GENUINE_V2_EVIDENCE_LIMITATION`
- **Implementation defect:** `NO` (the state machine executed the exact rules of ADR 0009)
- **Protocol dispatch defect:** `NO` (`--protocol v2` correctly bound and routed v2 semantics)
- **Reporting defect:** `NO` (`exit_empty_evidenced` correctly reflected the non-available record state)
- **Data repair required:** `NO` (upstream NZD market holiday closure returned genuine empty body)
- **Code repair required:** `NO`
- **Rerun under ADR 0009 justified:** `NO`

---

## 4. Research Conclusion

1. **Edge Status:** **UNDETERMINED**. Because neither v1 nor v2 produced a completed, valid P4 evaluation dataset across the frozen 2014–2023 research horizon, no statistical performance metrics (Sharpe, expectancy, drawdown, or HAC bounds) exist. Candidate C must not be described as profitable, unprofitable, `GO`, or `NO_GO`. No performance inference is permitted.
2. **Research Status:** **CLOSED / NOT_EVALUABLE**. Candidate C research is terminated.
3. **Artifact Permanence:** The result records for Run `88aed797` (v1) and Run `ed8a8e5f` (v2) are permanent, immutable audit artifacts. Neither protocol shall be rerun.
4. **No Ad-Hoc v3:** Creating a "Candidate C v3" solely to engineer an exit around the observed December 25 evidence condition is prohibited. Any future related cross-sectional hypothesis must be formulated as a distinct, prospectively motivated research candidate under a new ADR rather than a post-hoc patch to achieve evaluability.
5. **Sealed Boundary:** All 2024+ data remains strictly sealed.
