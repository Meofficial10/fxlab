# ADR 0012 — Candidate D v1 NO_GO closure

- **Status:** Accepted — Candidate D v1 CLOSED / NO_GO
- **Protocol:** `candidate_d_time_series_momentum.v1`
- **Frozen preregistration:** ADR 0011
- **Measurement-engine commit:** `de47d5c68a18dd5d3f53362294c82613f6d082bb`
- **Runner and measurement revision:** `bf781a6566808f1747bede6ef6a94045116def2f`
- **Run ID:** `67ee2f4bd638cab07b645cd7e1a253b0fef14adc5e29d40ebc8178e8b6f3792c`
- **Result ID:** `a05b0a322cfc38179145c5f9e26f5015d34076ea311903fe803ca645b9faec97`
- **Decision:** `NO_GO`

## 1. Decision

Candidate D v1 completed an evaluable measurement under the frozen ADR 0011
protocol. The preregistered economic and statistical P4 gate returned `NO_GO`.
Candidate D v1 is therefore **CLOSED / NO_GO**.

This outcome is distinct from Candidate C. Candidate C was `NOT_EVALUABLE`:
its performance evaluation could not validly complete. Candidate D was evaluable:
its evaluation completed, but it failed the preregistered P4 gate.

Infrastructure confidence remains separate from trading edge. Successful data,
provenance, measurement, and execution-safety infrastructure does not establish an
economic trading edge.

## 2. Canonical frozen failure reasons

The authoritative Candidate D v1 result records exactly these failure reasons:

1. `train_expectancy_not_strictly_positive`
2. `validation_net_return_not_strictly_positive`
3. `validation_sharpe_not_strictly_positive`
4. `validation_expectancy_not_strictly_positive`
5. `validation_lcb_not_strictly_positive`
6. `validation_2022_not_strictly_positive`
7. `validation_2023_not_strictly_positive`

No additional numerical performance values are asserted by this closure ADR.

## 3. Research closure and anti-tuning boundary

- Do not rerun the same Candidate D v1 protocol to seek a different result.
- Do not tune the 20-day lookback using this validation result.
- Do not tune the 20-`AVAILABLE`-session holding period using this validation result.
- Do not remove pairs based on observed Candidate D performance.
- Do not weaken costs, HAC inference, multiplicity control, or P4 thresholds.
- Any future related hypothesis must be prospectively distinct and preregistered
  before its performance is inspected.

ADR 0011 remains the frozen preregistration and is not amended by this closure.
ADR 0012 records its completed canonical outcome.

## 4. Authorization boundary

This result does not authorize opening or inspecting the sealed 2024+ period. The
2024+ test window remains sealed.

Candidate D does not authorize:

- machine learning;
- demo strategy deployment;
- live strategy deployment;
- real-money trading.

No automatic next research or execution action follows from this `NO_GO` decision.
